package main

import (
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"net"
	"os"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

type Stat struct {
	Port        int           `json:"port"`
	Phase       int           `json:"phase"`
	Packets     int64         `json:"packets"`
	Bytes       int64         `json:"bytes"`
	First       float64       `json:"first_s"`
	Last        float64       `json:"last_s"`
	DSCP        map[int]int64 `json:"dscp,omitempty"`
	Errors      int64         `json:"errors,omitempty"`
	Reordered   int64         `json:"reordered,omitempty"`
	SocketDrops uint32        `json:"socket_drops"`
	TTL         map[int]int64 `json:"ttl,omitempty"`
	Origins     map[int]int64 `json:"origins,omitempty"`
}

var portsArg = flag.String("ports", "45101,45102,45103", "")
var nonce = flag.Uint64("nonce", 1, "")
var mode = flag.String("mode", "server", "")
var addr = flag.String("addr", "192.0.2.3", "")
var lifetime = flag.Float64("lifetime", 20, "")
var trigger = flag.String("trigger", "", "Optional private file signaled by the first Windows main-phase packet")

func emit(x interface{}) { b, _ := json.Marshal(x); fmt.Println(string(b)) }
func serve() {
	start := time.Now()
	var wg sync.WaitGroup
	stats := make(chan []Stat, 3)
	var triggerOnce sync.Once
	for _, p := range ports() {
		c, e := net.ListenUDP("udp4", &net.UDPAddr{IP: net.ParseIP(*addr), Port: p})
		if e != nil {
			panic(e)
		}
		c.SetReadBuffer(8 << 20)
		enableTOS(c)
		c.SetReadDeadline(start.Add(time.Duration(*lifetime * float64(time.Second))))
		wg.Add(1)
		go func(port int, conn *net.UDPConn) {
			defer wg.Done()
			defer conn.Close()
			rows := map[int]*Stat{}
			lastSeq := map[int]uint64{}
			buf := make([]byte, 2048)
			oob := make([]byte, 256)
			for {
				n, on, _, peer, e := conn.ReadMsgUDP(buf, oob)
				if e != nil {
					break
				}
				if n < 24 || string(buf[:4]) != "HGQV" || binary.BigEndian.Uint64(buf[16:24]) != *nonce {
					continue
				}
				phase := int(buf[4])
				origin := int(buf[5])
				if phase > 2 {
					continue
				}
				if phase == 1 && origin == 0 && *trigger != "" {
					triggerOnce.Do(func() {
						if e := os.WriteFile(*trigger, []byte(strconv.FormatInt(time.Now().UnixNano(), 10)), 0600); e != nil {
							panic(e)
						}
					})
				}
				st := rows[phase]
				if st == nil {
					st = &Stat{Port: port, Phase: phase, First: time.Since(start).Seconds(), DSCP: map[int]int64{}, TTL: map[int]int64{}, Origins: map[int]int64{}}
					rows[phase] = st
				}
				sq := binary.BigEndian.Uint64(buf[8:16])
				if sq <= lastSeq[origin] {
					st.Reordered++
				}
				lastSeq[origin] = sq
				st.Packets++
				st.Bytes += int64(n)
				st.Last = time.Since(start).Seconds()
				tos, ttl, drops := readMeta(oob[:on])
				st.DSCP[tos>>2]++
				st.TTL[ttl]++
				st.Origins[origin]++
				if drops > st.SocketDrops {
					st.SocketDrops = drops
				}
				if phase == 0 && st.Packets%32 == 1 {
					conn.WriteToUDP(buf[:24], peer)
				}
			}
			out := []Stat{}
			for i := 0; i <= 2; i++ {
				if rows[i] != nil {
					out = append(out, *rows[i])
				}
			}
			stats <- out
		}(p, c)
	}
	fmt.Println("READY")
	wg.Wait()
	close(stats)
	all := []Stat{}
	for s := range stats {
		all = append(all, s...)
	}
	emit(all)
}
func ports() []int {
	out := []int{}
	for _, s := range strings.Split(*portsArg, ",") {
		p, e := strconv.Atoi(s)
		if e != nil || p < 1024 || p > 65535 {
			panic("invalid test port")
		}
		out = append(out, p)
	}
	if len(out) != 3 {
		panic("three ports required")
	}
	return out
}
func main() {
	runtime.GOMAXPROCS(4)
	flag.Parse()
	if *lifetime < 1 || *lifetime > 40 {
		panic("test duration/rate out of bounds")
	}
	if *mode == "server" {
		serve()
	} else if *mode == "background" {
		sendBackground()
	} else {
		fmt.Fprintln(os.Stderr, "mode")
		os.Exit(1)
	}
}
