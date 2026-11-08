// Linux endpoint for production-policy tests. No classification rules are installed.
package main

import (
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"
	"unsafe"
)

type Sample struct {
	Case        int           `json:"case"`
	Phase       int           `json:"phase"`
	Packets     int64         `json:"packets"`
	Bytes       int64         `json:"bytes"`
	DSCP        map[int]int64 `json:"dscp"`
	SocketDrops uint32        `json:"socket_drops"`
}
type Result struct {
	Samples []*Sample `json:"samples"`
	Errors  []string  `json:"errors"`
}

var mu sync.Mutex
var rows = map[[2]int]*Sample{}
var errors []string
var nonce uint64
var addr string
var port int

func header(b []byte) (int, int, bool) {
	if len(b) < 24 {
		return 0, 0, false
	}
	return int(b[6]), int(b[4]), string(b[:4]) == "HGQS" && binary.BigEndian.Uint64(b[16:24]) == nonce
}
func record(b []byte, tos int, drops uint32) {
	if len(b) < 24 {
		return
	}
	id, phase, ok := header(b)
	if !ok || phase > 1 {
		return
	}
	mu.Lock()
	defer mu.Unlock()
	k := [2]int{id, phase}
	s := rows[k]
	if s == nil {
		s = &Sample{Case: id, Phase: phase, DSCP: map[int]int64{}}
		rows[k] = s
	}
	s.Packets++
	s.Bytes += int64(len(b))
	s.DSCP[tos>>2]++
	if drops > s.SocketDrops {
		s.SocketDrops = drops
	}
}
func errRecord(err error) { mu.Lock(); errors = append(errors, err.Error()); mu.Unlock() }

func udp(c *net.UDPConn, stop <-chan struct{}) {
	buf, oob := make([]byte, 65536), make([]byte, 256)
	for {
		c.SetReadDeadline(time.Now().Add(300 * time.Millisecond))
		n, on, flags, peer, e := c.ReadMsgUDP(buf, oob)
		if e != nil {
			select {
			case <-stop:
				return
			default:
			}
			if x, ok := e.(net.Error); ok && x.Timeout() {
				continue
			}
			return
		}
		if n < 24 {
			continue
		}
		_, _, ok := header(buf[:n])
		if !ok {
			continue
		}
		if flags&(syscall.MSG_TRUNC|syscall.MSG_CTRUNC) != 0 {
			errRecord(fmt.Errorf("truncated UDP observation"))
			continue
		}
		tos := -4
		var drops uint32
		cms, e := syscall.ParseSocketControlMessage(oob[:on])
		if e != nil {
			errRecord(e)
			continue
		}
		for _, cm := range cms {
			if cm.Header.Level == syscall.IPPROTO_IP && cm.Header.Type == syscall.IP_TOS && len(cm.Data) > 0 {
				tos = int(cm.Data[0])
			}
			if cm.Header.Level == syscall.SOL_SOCKET && cm.Header.Type == 40 && len(cm.Data) >= 4 {
				drops = binary.NativeEndian.Uint32(cm.Data)
			}
		}
		record(buf[:n], tos, drops)
		// Bulk main traffic is one-way. Warmup replies establish both directions.
		if buf[5] == 0 || (buf[4] == 0 && binary.BigEndian.Uint64(buf[8:16])%32 == 1) {
			if _, e = c.WriteToUDP(buf[:n], peer); e != nil {
				errRecord(e)
			}
		}
	}
}
func tcp(l *net.TCPListener, stop <-chan struct{}, wg *sync.WaitGroup) {
	for {
		l.SetDeadline(time.Now().Add(300 * time.Millisecond))
		c, e := l.AcceptTCP()
		if e != nil {
			select {
			case <-stop:
				return
			default:
			}
			if x, ok := e.(net.Error); ok && x.Timeout() {
				continue
			}
			return
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer c.Close()
			c.SetNoDelay(true)
			c.SetDeadline(time.Now().Add(12 * time.Second))
			buf := make([]byte, 1400)
			for {
				if _, e := io.ReadFull(c, buf[:24]); e != nil {
					return
				}
				size := int(binary.BigEndian.Uint16(buf[12:14]))
				if size < 24 || size > 1400 {
					return
				}
				if _, e := io.ReadFull(c, buf[24:size]); e != nil {
					return
				}
				if _, _, ok := header(buf[:size]); !ok {
					return
				}
				if _, e := c.Write(buf[:size]); e != nil {
					return
				}
			}
		}()
	}
}

func rawTCP(iface string, stop <-chan struct{}) (func(), error) {
	dev, e := net.InterfaceByName(iface)
	if e != nil {
		return nil, e
	}
	fd, e := syscall.Socket(syscall.AF_PACKET, syscall.SOCK_RAW, 0x0008)
	if e != nil {
		return nil, e
	}
	fail := func(e error) (func(), error) { syscall.Close(fd); return nil, e }
	if e = syscall.Bind(fd, &syscall.SockaddrLinklayer{Ifindex: dev.Index, Protocol: 0x0008}); e != nil {
		return fail(e)
	}
	// Kernel filter: IPv4 TCP only, avoiding extra userspace work for UDP loads.
	filters := []syscall.SockFilter{{Code: 0x30, K: 23}, {Code: 0x15, Jt: 0, Jf: 1, K: 6}, {Code: 0x06, K: 65535}, {Code: 0x06, K: 0}}
	fprog := syscall.SockFprog{Len: uint16(len(filters)), Filter: &filters[0]}
	_, _, eno := syscall.Syscall6(syscall.SYS_SETSOCKOPT, uintptr(fd), syscall.SOL_SOCKET, syscall.SO_ATTACH_FILTER, uintptr(unsafe.Pointer(&fprog)), unsafe.Sizeof(fprog), 0)
	if eno != 0 {
		return fail(eno)
	}
	syscall.SetsockoptTimeval(fd, syscall.SOL_SOCKET, syscall.SO_RCVTIMEO, &syscall.Timeval{Usec: 300000})
	finished := make(chan struct{})
	go func() {
		defer close(finished)
		buf := make([]byte, 65536)
		local := net.ParseIP(addr).To4()
		for {
			select {
			case <-stop:
				return
			default:
			}
			n, _, e := syscall.Recvfrom(fd, buf, 0)
			if e != nil {
				if e == syscall.EAGAIN || e == syscall.EINTR {
					continue
				}
				return
			}
			if n < 54 || buf[14]>>4 != 4 || buf[23] != 6 {
				continue
			}
			ihl := int(buf[14]&15) * 4
			off := 14 + ihl
			if off+20 > n || !net.IP(buf[30:34]).Equal(local) || int(binary.BigEndian.Uint16(buf[off+2:off+4])) != port {
				continue
			}
			data := off + int(buf[off+12]>>4)*4
			end := 14 + int(binary.BigEndian.Uint16(buf[16:18]))
			if end > n || data+24 > end {
				continue
			}
			record(buf[data:end], int(buf[15]), 0)
		}
	}()
	return func() { syscall.Close(fd); <-finished }, nil
}

func main() {
	flag.StringVar(&addr, "addr", "", "local address")
	flag.IntVar(&port, "port", 0, "dedicated UDP/TCP port")
	flag.Uint64Var(&nonce, "nonce", 0, "run token")
	iface := flag.String("iface", "", "receive interface")
	lifetime := flag.Int("lifetime", 150, "bounded lifetime")
	flag.Parse()
	if net.ParseIP(addr).To4() == nil || port < 1024 || port > 65535 || *lifetime < 1 || *lifetime > 180 {
		panic("invalid probe limits")
	}
	c, e := net.ListenUDP("udp4", &net.UDPAddr{IP: net.ParseIP(addr), Port: port})
	if e != nil {
		panic(e)
	}
	c.SetReadBuffer(8 << 20)
	raw, e := c.SyscallConn()
	if e != nil {
		panic(e)
	}
	var se error
	raw.Control(func(fd uintptr) {
		se = syscall.SetsockoptInt(int(fd), syscall.IPPROTO_IP, syscall.IP_RECVTOS, 1)
		if se == nil {
			se = syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, 40, 1)
		}
	})
	if se != nil {
		panic(se)
	}
	l, e := net.ListenTCP("tcp4", &net.TCPAddr{IP: net.ParseIP(addr), Port: port})
	if e != nil {
		panic(e)
	}
	done := make(chan os.Signal, 1)
	signal.Notify(done, syscall.SIGINT, syscall.SIGTERM)
	stop := make(chan struct{})
	var wg sync.WaitGroup
	closeRaw, e := rawTCP(*iface, stop)
	if e != nil {
		panic(e)
	}
	wg.Add(2)
	go func() { defer wg.Done(); udp(c, stop) }()
	go func() { defer wg.Done(); tcp(l, stop, &wg) }()
	fmt.Println("READY")
	select {
	case <-done:
	case <-time.After(time.Duration(*lifetime) * time.Second):
	}
	close(stop)
	c.Close()
	l.Close()
	closeRaw()
	wg.Wait()
	out := Result{Errors: errors}
	for _, s := range rows {
		out.Samples = append(out.Samples, s)
	}
	b, _ := json.Marshal(out)
	fmt.Println(string(b))
}
