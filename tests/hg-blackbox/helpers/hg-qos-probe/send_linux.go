package main

import (
	"context"
	"encoding/binary"
	"flag"
	"net"
	"os"
	"strconv"
	"strings"
	"syscall"
	"time"
)

var extraPort = flag.Int("port", 45103, "Single background destination port")
var extraSource = flag.String("source", "192.0.2.252", "Source identity address")
var extraSport = flag.Int("source-port", 55000, "Unique source port")
var extraDevice = flag.String("device", "eth0", "Bind sender to the existing LAN interface")
var extraRate = flag.Float64("mbps", 400, "Additional background payload rate")
var extraSeconds = flag.Float64("seconds", 2.5, "Main window, anchored to receiver trigger")

type SendStat struct {
	Stat
	SendCalls      int64   `json:"send_calls"`
	SendSeconds    float64 `json:"send_seconds"`
	MaxSendSeconds float64 `json:"max_send_seconds"`
	Batch          int     `json:"send_batch"`
}

func sendBackground() {
	if *trigger == "" || *extraSeconds <= 0 || *extraSeconds > 3 || *extraRate < 100 || *extraRate > 600 ||
		*extraPort < 1024 || *extraPort > 65535 || *extraSport < 1024 || *extraSport > 65535 || *extraDevice != "eth0" {
		panic("bounded background sender arguments required")
	}
	dialer := net.Dialer{LocalAddr: &net.UDPAddr{IP: net.ParseIP(*extraSource), Port: *extraSport},
		Control: func(network, address string, raw syscall.RawConn) error {
			var inner error
			e := raw.Control(func(fd uintptr) {
				inner = syscall.SetsockoptString(int(fd), syscall.SOL_SOCKET, syscall.SO_BINDTODEVICE, *extraDevice)
			})
			if e != nil {
				return e
			}
			return inner
		}}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	connection, e := dialer.DialContext(ctx, "udp4", net.JoinHostPort(*addr, strconv.Itoa(*extraPort)))
	if e != nil {
		panic(e)
	}
	c := connection.(*net.UDPConn)
	defer c.Close()
	c.SetWriteBuffer(4 << 20)
	raw, _ := c.SyscallConn()
	var setErr error
	raw.Control(func(fd uintptr) { setErr = syscall.SetsockoptInt(int(fd), syscall.IPPROTO_UDP, 103, 1400) }) // UDP_SEGMENT
	if setErr != nil {
		panic(setErr)
	}
	start := time.Now()
	deadline := start.Add(22 * time.Second)
	next := start
	rows := []SendStat{{Stat: Stat{Port: *extraPort, Phase: 0}, Batch: 1}, {Stat: Stat{Port: *extraPort, Phase: 1}, Batch: 32}}
	sequence := uint64(0)
	phase := 0
	var signaled time.Time
	delay := float64(0)
	packets := [][]byte{make([]byte, 1400), make([]byte, 1400*32)}
	for p, b := range packets {
		for o := 0; o < len(b); o += 1400 {
			copy(b[o:o+4], "HGQV")
			b[o+4] = byte(p)
			b[o+5] = 1
			binary.BigEndian.PutUint64(b[o+16:o+24], *nonce)
		}
	}
	for time.Now().Before(deadline) {
		if phase == 0 {
			b, e := os.ReadFile(*trigger)
			if e == nil {
				n, err := strconv.ParseInt(strings.TrimSpace(string(b)), 10, 64)
				if err == nil && n > 0 {
					phase = 1
					signaled = time.Unix(0, n)
					deadline = signaled.Add(time.Duration(*extraSeconds * float64(time.Second)))
					next = time.Now()
					delay = next.Sub(signaled).Seconds()
					rows[1].First = next.Sub(start).Seconds()
				}
			}
		}
		if wait := time.Until(next); wait > 0 {
			if wait > 150*time.Microsecond {
				time.Sleep(wait - 100*time.Microsecond)
			}
			continue
		}
		b := packets[phase]
		for o := 0; o < len(b); o += 1400 {
			sequence++
			binary.BigEndian.PutUint64(b[o+8:o+16], sequence)
		}
		c.SetWriteDeadline(time.Now().Add(500 * time.Millisecond))
		sending := time.Now()
		n, err := c.Write(b)
		elapsed := time.Since(sending).Seconds()
		row := &rows[phase]
		row.SendCalls++
		row.SendSeconds += elapsed
		if elapsed > row.MaxSendSeconds {
			row.MaxSendSeconds = elapsed
		}
		if err != nil || n != len(b) {
			row.Errors++
		} else {
			row.Packets += int64(row.Batch)
			row.Bytes += int64(n)
		}
		row.Last = time.Since(start).Seconds()
		rate := float64(10)
		if phase == 1 {
			rate = *extraRate
		}
		next = next.Add(time.Duration(float64(len(b)) * 8 / (rate * 1e6) * float64(time.Second)))
		if time.Since(next) > 3*time.Millisecond {
			next = time.Now()
		}
	}
	if phase != 1 {
		panic("receiver trigger was not observed")
	}
	emit(map[string]interface{}{"rows": rows, "source": c.LocalAddr().String(), "trigger_unix_nanos": signaled.UnixNano(), "start_delay_seconds": delay})
}
