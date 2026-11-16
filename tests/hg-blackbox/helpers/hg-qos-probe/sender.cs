using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Threading;

namespace HgQoS {
    public class Sample {
        public int port;
        public int phase;
        public long packets;
        public long bytes;
        public long errors;
        public double first_s;
        public double last_s;
        public int send_batch;
        public long send_calls;
        public double send_seconds;
        public double max_send_seconds;
    }
    public static class Probe {
        private static void Put64(byte[] b, int offset, ulong value) {
            for (int i = 0; i < 8; i++) b[offset + i] = (byte)(value >> (56 - 8*i));
        }
        public static Sample[] Run(string address, int[] ports, int source, double mbps, double seconds, double warmup, ulong nonce) {
            return RunBatch(address, ports, source, mbps, seconds, warmup, nonce, 1);
        }
        public static Sample[] RunBatch(string address, int[] ports, int source, double mbps, double seconds, double warmup, ulong nonce, int segments) {
            if (ports.Length != 3 || mbps <= 0 || mbps > 1000 || seconds <= 0 || seconds > 5 || warmup < 1 || warmup > 10)
                throw new ArgumentException("Bounded three-stream test required");
            if (segments < 1 || segments > 32) throw new ArgumentException("UDP batch must be 1..32");
            var output = new Sample[9];
            var workers = new Thread[3];
            var failures = new Exception[3];
            long start = Stopwatch.GetTimestamp() + Stopwatch.Frequency / 4;
            for (int stream = 0; stream < 3; stream++) {
                int index = stream;
                workers[index] = new Thread(() => {
                    try {
                        using (var socket = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp)) {
                            socket.SendBufferSize = 4 * 1024 * 1024;
                            socket.ReceiveBufferSize = 1024 * 1024;
                            socket.SendTimeout = 1000;
                            socket.Bind(new IPEndPoint(IPAddress.Any, source + index));
                            socket.Connect(new IPEndPoint(IPAddress.Parse(address), ports[index]));
                            // Windows UDP_SEND_MSG_SIZE: individual on-wire datagrams remain 1400 bytes.
                            if (segments > 1) socket.SetSocketOption(SocketOptionLevel.Udp, (SocketOptionName)2, 1400);
                            ulong sequence = 0;
                            double phaseStart = start;
                            double[] lengths = {warmup, seconds, .25};
                            double[] rates = {10, mbps, 2};
                            for (int phase = 0; phase < 3; phase++) {
                                int batch = phase == 1 ? segments : 1;
                                byte[] packet = new byte[1400 * batch];
                                for (int segment = 0; segment < batch; segment++) {
                                    int offset = segment * 1400;
                                    packet[offset] = 72; packet[offset+1] = 71; packet[offset+2] = 81; packet[offset+3] = 86;
                                    packet[offset+4] = (byte)phase;
                                    Put64(packet, offset+16, nonce);
                                }
                                var row = new Sample {port = ports[index], phase = phase, first_s = (phaseStart-start)/Stopwatch.Frequency, send_batch = batch};
                                double end = phaseStart + lengths[phase] * Stopwatch.Frequency;
                                double next = phaseStart;
                                double interval = packet.Length * 8.0 * Stopwatch.Frequency / (rates[phase] * 1000000.0);
                                while (Stopwatch.GetTimestamp() < end) {
                                    long now = Stopwatch.GetTimestamp();
                                    if (now < next) {
                                        if (rates[phase] <= 100 && next-now > Stopwatch.Frequency / 500) Thread.Sleep(1);
                                        else Thread.SpinWait(10);
                                        continue;
                                    }
                                    for (int segment = 0; segment < batch; segment++) Put64(packet, segment*1400+8, ++sequence);
                                    long sending = Stopwatch.GetTimestamp();
                                    try {
                                        int n = socket.Send(packet);
                                        if (n != packet.Length) throw new InvalidOperationException("Partial UDP batch send");
                                        row.packets += batch; row.bytes += n;
                                    }
                                    catch (SocketException) { row.errors++; }
                                    double sendElapsed = (Stopwatch.GetTimestamp()-sending)/(double)Stopwatch.Frequency;
                                    row.send_calls++; row.send_seconds += sendElapsed;
                                    if (sendElapsed > row.max_send_seconds) row.max_send_seconds = sendElapsed;
                                    next += interval;
                                    now = Stopwatch.GetTimestamp();
                                    if (now-next > Stopwatch.Frequency * .003) next = now;
                                    row.last_s = (now-start)/(double)Stopwatch.Frequency;
                                }
                                output[index*3 + phase] = row;
                                phaseStart = end;
                            }
                        }
                    } catch (Exception e) { failures[index] = e; }
                });
                workers[index].IsBackground = true;
                workers[index].Start();
            }
            foreach (Thread worker in workers) {
                if (!worker.Join((int)((warmup+seconds+4)*1000))) throw new TimeoutException("Sender deadline exceeded");
            }
            foreach (Exception e in failures) if (e != null) throw e;
            return output;
        }
    }
}
