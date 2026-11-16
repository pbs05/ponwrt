package main

import (
	"encoding/json"
	"flag"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"testing"
	"time"
)

func TestStreamCLIHelper(t *testing.T) {
	if os.Getenv("HG_STREAM_CLI_TEST") != "1" {
		return
	}
	for i, arg := range os.Args {
		if arg == "--" {
			os.Args = append([]string{os.Args[0]}, os.Args[i+1:]...)
			break
		}
	}
	flag.CommandLine = flag.NewFlagSet(os.Args[0], flag.ExitOnError)
	main()
	os.Exit(0)
}

func TestWallDeadlineIncludesHeaderWait(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(500 * time.Millisecond)
		w.WriteHeader(200)
	}))
	defer server.Close()
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	command := exec.Command(executable, "-test.run=^TestStreamCLIHelper$", "--",
		"-url", server.URL, "-source", "127.0.0.1", "-wall-seconds", ".15")
	command.Env = append(os.Environ(), "HG_STREAM_CLI_TEST=1")
	began := time.Now()
	data, err := command.Output()
	if err == nil || time.Since(began) > 450*time.Millisecond {
		t.Fatalf("wall deadline did not cover headers: %v %s", err, data)
	}
	var result map[string]any
	if json.Unmarshal(data, &result) != nil || result["error"] == nil {
		t.Fatalf("missing bounded error result: %s", data)
	}
}

func packet(cc byte) []byte {
	p := make([]byte, 188)
	p[0], p[1], p[2], p[3] = 0x47, 0, 42, 0x10|cc
	return p
}

func TestTSContinuityAndDuplicate(t *testing.T) {
	var good tracker
	for i := 0; i < 40; i++ {
		good.feed(packet(byte(i & 15)))
	}
	if good.gaps != 0 || good.duplicates != 0 || good.packets != 40 {
		t.Fatal(good)
	}
	good.feed(packet(7))
	if good.duplicates != 1 {
		t.Fatal("duplicate not recorded")
	}
	good.feed(packet(9))
	if good.gaps != 1 {
		t.Fatal("missing CC not recorded")
	}
}

func TestTSSplitAndInitialAlignment(t *testing.T) {
	var stream []byte
	for i := 0; i < 20; i++ {
		stream = append(stream, packet(byte(i&15))...)
	}
	stream = append([]byte{0, 0, 0}, stream...)
	var r tracker
	for len(stream) > 0 {
		n := 17
		if len(stream) < n {
			n = len(stream)
		}
		r.feed(stream[:n])
		stream = stream[n:]
	}
	if r.resync != 3 || r.gaps != 0 || r.packets != 20 {
		t.Fatal(r)
	}
}

func TestTSErrorAndDiscontinuity(t *testing.T) {
	var r tracker
	r.feed(packet(0))
	p := packet(9)
	p[3] = 0x39
	p[4] = 1
	p[5] = 0x80
	r.feed(p)
	if r.gaps != 0 {
		t.Fatal("declared discontinuity counted as loss")
	}
	p = packet(10)
	p[1] |= 0x80
	r.feed(p)
	if r.tei != 1 {
		t.Fatal("TEI not counted")
	}
	p = packet(11)
	p[3] = 0
	r.feed(p)
	if r.invalid != 1 {
		t.Fatal("invalid adaptation control not counted")
	}
}

func TestWarmupByte47DoesNotFakePacketBoundary(t *testing.T) {
	prefix := make([]byte, 57)
	prefix[0] = 0x47
	stream := append(prefix, packet(0)...)
	stream = append(stream, packet(1)...)
	stream = append(stream, packet(2)...)
	var r tracker
	r.feed(stream)
	if r.invalid != 0 || r.resync != 57 || r.packets != 3 {
		t.Fatal(r)
	}
}
