package main

import (
	"encoding/binary"
	"net"
	"syscall"
)

func enableTOS(c *net.UDPConn) {
	raw, _ := c.SyscallConn()
	raw.Control(func(fd uintptr) {
		syscall.SetsockoptInt(int(fd), syscall.IPPROTO_IP, syscall.IP_RECVTOS, 1)
		syscall.SetsockoptInt(int(fd), syscall.IPPROTO_IP, syscall.IP_RECVTTL, 1)
		syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, 40, 1)
	})
}
func readMeta(oob []byte) (int, int, uint32) {
	tos := -4
	ttl := -1
	var drops uint32
	msgs, _ := syscall.ParseSocketControlMessage(oob)
	for _, m := range msgs {
		if m.Header.Level == syscall.IPPROTO_IP && m.Header.Type == syscall.IP_TOS && len(m.Data) > 0 {
			tos = int(m.Data[0])
		}
		if m.Header.Level == syscall.SOL_SOCKET && m.Header.Type == 40 && len(m.Data) >= 4 {
			drops = binary.NativeEndian.Uint32(m.Data)
		}
		if m.Header.Level == syscall.IPPROTO_IP && m.Header.Type == syscall.IP_TTL && len(m.Data) >= 4 {
			ttl = int(binary.NativeEndian.Uint32(m.Data))
		}
	}
	return tos, ttl, drops
}
