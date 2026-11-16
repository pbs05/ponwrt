// SPDX-License-Identifier: GPL-2.0-only
// Bounded MPEG-TS HTTP receiver; no proxy, redirect, decoding or media recording.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"os"
	"time"
)

type tracker struct {
	synced                                          bool
	buf                                             []byte
	seen                                            [8192]bool
	cc                                              [8192]byte
	hash                                            [8192]uint64
	packets, gaps, duplicates, tei, resync, invalid int64
}

func (t *tracker) feed(b []byte) {
	t.buf = append(t.buf, b...)
	for len(t.buf) >= 188 {
		if !t.synced || t.buf[0] != 0x47 {
			t.synced = false
			n := -1
			for i := 0; i+376 < len(t.buf); i++ {
				if t.buf[i] == 0x47 && t.buf[i+188] == 0x47 && t.buf[i+376] == 0x47 {
					n = i
					break
				}
			}
			if n < 0 {
				if len(t.buf) > 564 {
					t.resync += int64(len(t.buf) - 564)
					t.buf = t.buf[len(t.buf)-564:]
				}
				break
			}
			t.resync += int64(n)
			t.buf = t.buf[n:]
			t.synced = true
			continue
		}
		p := t.buf[:188]
		t.buf = t.buf[188:]
		t.packets++
		if p[1]&0x80 != 0 {
			t.tei++
		}
		pid := int(p[1]&31)<<8 | int(p[2])
		af := (p[3] >> 4) & 3
		if af == 0 || (af&2 != 0 && (p[4] > 183 || (af == 3 && p[4] > 182))) {
			t.invalid++
			continue
		}
		if pid == 8191 {
			continue
		}
		if af&2 != 0 && p[4] > 0 && p[5]&0x80 != 0 {
			t.seen[pid] = false
		}
		if af&1 == 0 {
			continue
		}
		cc := p[3] & 15
		h := uint64(1469598103934665603)
		for _, v := range p {
			h = (h ^ uint64(v)) * 1099511628211
		}
		if t.seen[pid] {
			if cc == t.cc[pid] && h == t.hash[pid] {
				t.duplicates++
			} else if cc != (t.cc[pid]+1)&15 {
				t.gaps++
			}
		}
		t.seen[pid] = true
		t.cc[pid] = cc
		t.hash[pid] = h
	}
}

func main() {
	url := flag.String("url", "", "stream URL")
	source := flag.String("source", "", "local IPv4")
	seconds := flag.Int("seconds", 45, "measured seconds")
	warm := flag.Int("warmup", 10, "warmup seconds")
	wall := flag.Float64("wall-seconds", 0, "optional request lifetime, including connection/header wait")
	flag.Parse()
	if *seconds < 1 || *seconds > 180 || *warm < 0 || *warm > 60 || *wall < 0 || *wall > 180 || math.IsNaN(*wall) || math.IsInf(*wall, 0) || net.ParseIP(*source).To4() == nil || *url == "" {
		fmt.Fprintln(os.Stderr, "invalid stream arguments")
		os.Exit(2)
	}
	d := &net.Dialer{Timeout: 8 * time.Second, LocalAddr: &net.TCPAddr{IP: net.ParseIP(*source)}}
	tr := &http.Transport{Proxy: nil, DialContext: d.DialContext, ResponseHeaderTimeout: 12 * time.Second, DisableCompression: true}
	client := &http.Client{Transport: tr, CheckRedirect: func(req *http.Request, via []*http.Request) error { return fmt.Errorf("unexpected redirect") }}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(*seconds+*warm+20)*time.Second)
	defer cancel()
	start := time.Now()
	var wallDeadline time.Time
	if *wall > 0 {
		wallDeadline = start.Add(time.Duration(*wall * float64(time.Second)))
		var wallCancel context.CancelFunc
		ctx, wallCancel = context.WithDeadline(ctx, wallDeadline)
		defer wallCancel()
	}
	req, err := http.NewRequestWithContext(ctx, "GET", *url, nil)
	if err != nil {
		panic(err)
	}
	resp, err := client.Do(req)
	if err != nil {
		json.NewEncoder(os.Stdout).Encode(map[string]any{"error": err.Error()})
		os.Exit(1)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		json.NewEncoder(os.Stdout).Encode(map[string]any{"http": resp.StatusCode, "body": string(b)})
		os.Exit(1)
	}
	ttfb := time.Since(start).Seconds()
	begin := time.Now()
	measured := begin.Add(time.Duration(*warm) * time.Second)
	deadline := measured.Add(time.Duration(*seconds) * time.Second)
	if !wallDeadline.IsZero() && wallDeadline.Before(deadline) {
		deadline = wallDeadline
	}
	timer := time.AfterFunc(time.Until(deadline), cancel)
	defer timer.Stop()
	buf := make([]byte, 65536)
	var t tracker
	var total int64
	var maxgap float64
	per := make([]int64, *seconds+1)
	last := measured
	var readErr string
	for {
		n, e := resp.Body.Read(buf)
		now := time.Now()
		if now.After(measured) && n > 0 {
			gap := now.Sub(last).Seconds()
			if gap > maxgap {
				maxgap = gap
			}
			last = now
			total += int64(n)
			sec := int(now.Sub(measured).Seconds())
			if sec < len(per) {
				per[sec] += int64(n)
			}
			t.feed(buf[:n])
		}
		if e != nil {
			if now.Before(deadline.Add(-500 * time.Millisecond)) {
				readErr = e.Error()
			}
			break
		}
		if !now.Before(deadline) {
			break
		}
	}
	elapsed := time.Since(measured).Seconds()
	if tailGap := time.Since(last).Seconds(); tailGap > maxgap {
		maxgap = tailGap
	}
	if elapsed < 0 {
		elapsed = 0
	}
	mbps := 0.0
	if elapsed > 0 {
		mbps = float64(total) * 8 / elapsed / 1e6
	}
	// A short initial byte offset is normal when a warmup ends between TS packets.
	out := map[string]any{"http": resp.StatusCode, "seconds": elapsed, "warmup_seconds": *warm, "bytes": total, "mbps": mbps, "ttfb_seconds": ttfb, "max_read_gap_seconds": maxgap, "bytes_per_second": per, "ts_packets": t.packets, "ts_cc_errors": t.gaps, "ts_duplicates": t.duplicates, "ts_tei": t.tei, "ts_resync_bytes": t.resync, "ts_invalid_headers": t.invalid, "read_error": readErr, "content_type": resp.Header.Get("Content-Type")}
	encoded, _ := json.Marshal(out)
	fmt.Println(string(bytes.TrimSpace(encoded)))
	if readErr != "" || total == 0 {
		os.Exit(1)
	}
}
