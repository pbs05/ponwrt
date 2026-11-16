using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Threading;

namespace HgQoSScenario {
    public class Case {
        public int Id, SourcePort, Port, Size = 128, Group;
        public string Name, Address, Protocol = "udp";
        public double RateMbps = 0.1024, Seconds = 2;
        public bool Bulk;
    }
    public class Tx {
        public int Case, Phase;
        public long Packets, Bytes, Replies, Errors;
        public List<double> RTTms = new List<double>();
    }
    public class Wire {
        public int Case, Phase;
        public long Packets;
        public Dictionary<string,long> DSCP = new Dictionary<string,long>();
    }
    public class Output {
        public List<Tx> Sent = new List<Tx>();
        public List<Wire> Received = new List<Wire>();
        public List<string> Errors = new List<string>();
    }
    public static class Client {
        static readonly object Gate = new object();
        static ulong Token;
        static volatile bool Stop;
        static Dictionary<int,Wire> WireRows;
        static ulong Get64(byte[] b,int p) { ulong v=0; for(int i=0;i<8;i++)v=(v<<8)|b[p+i];return v; }
        static void Put64(byte[] b,int p,ulong v) { for(int i=0;i<8;i++)b[p+i]=(byte)(v>>(56-8*i)); }
        static bool Header(byte[] b,int p,int n) { return n>=24 && b[p]==72 && b[p+1]==71 && b[p+2]==81 && b[p+3]==83 && Get64(b,p+16)==Token; }
        static byte[] Packet(Case c,int phase,ulong seq) {
            var b=new byte[c.Size]; b[0]=72;b[1]=71;b[2]=81;b[3]=83;b[4]=(byte)phase;b[5]=(byte)(c.Bulk?1:0);b[6]=(byte)c.Id;
            Put64(b,8,seq);Put64(b,16,Token);Put64(b,24,(ulong)Stopwatch.GetTimestamp());
            if(c.Protocol=="tcp"){b[12]=(byte)(b.Length>>8);b[13]=(byte)b.Length;}
            return b;
        }
        static void Sniff(Socket raw,Dictionary<int,Case> cases,Output output) {
            var b=new byte[65536];
            while(!Stop) {
                int n;
                try{n=raw.Receive(b);}catch(SocketException e){if(e.SocketErrorCode==SocketError.TimedOut)continue;if(!Stop)lock(Gate)output.Errors.Add("raw: "+e.Message);break;}catch(ObjectDisposedException){break;}
                if(n<48||b[0]>>4!=4)continue;
                int h=(b[0]&15)*4,proto=b[9],p;
                if(proto==17)p=h+8;else if(proto==6){if(h+20>n)continue;p=h+(b[h+12]>>4)*4;}else continue;
                if(p+24>n||!Header(b,p,n-p))continue;
                int id=b[p+6],phase=b[p+4];Case c;
                if(phase>1||!cases.TryGetValue(id,out c)||((b[h+2]<<8)|b[h+3])!=c.SourcePort)continue;
                byte[] a=IPAddress.Parse(c.Address).GetAddressBytes();bool same=true;
                for(int i=0;i<4;i++)same&=a[i]==b[12+i];if(!same)continue;
                lock(Gate) {
                    Wire r;int key=id*2+phase;
                    if(!WireRows.TryGetValue(key,out r)){r=new Wire{Case=id,Phase=phase};WireRows[key]=r;}
                    r.Packets++;string ds=(b[1]>>2).ToString();long count;r.DSCP.TryGetValue(ds,out count);r.DSCP[ds]=count+1;
                }
            }
        }
        static void RunUDP(Case c,string local,Output output,long start) {
            var rows=new Tx[]{new Tx{Case=c.Id,Phase=0},new Tx{Case=c.Id,Phase=1}};
            using(var s=new Socket(AddressFamily.InterNetwork,SocketType.Dgram,ProtocolType.Udp)) {
                s.ReceiveBufferSize=4<<20;s.SendBufferSize=4<<20;s.ReceiveTimeout=200;s.SendTimeout=1000;
                s.Bind(new IPEndPoint(IPAddress.Parse(local),c.SourcePort));s.Connect(IPAddress.Parse(c.Address),c.Port);
                bool endRx=false;
                var rx=new Thread(()=>{var b=new byte[2048];while(!Volatile.Read(ref endRx)){int n;try{n=s.Receive(b);}catch(SocketException e){if(e.SocketErrorCode==SocketError.TimedOut)continue;break;}catch(ObjectDisposedException){break;}
                    if(!Header(b,0,n)||b[6]!=c.Id||b[4]>1)continue;int phase=b[4];lock(rows){rows[phase].Replies++;if(!c.Bulk)rows[phase].RTTms.Add((Stopwatch.GetTimestamp()-(long)Get64(b,24))*1000.0/Stopwatch.Frequency);}
                }});rx.IsBackground=true;rx.Start();
                ulong sequence=0;double phaseStart=start;var packet=Packet(c,0,0);
                for(int phase=0;phase<2;phase++) {
                    double rate=phase==0?(c.Bulk?10:0.2):c.RateMbps;
                    double end=phaseStart+(phase==0?1:c.Seconds)*Stopwatch.Frequency,next=phaseStart;
                    double step=c.Size*8.0*Stopwatch.Frequency/(rate*1e6);
                    packet[4]=(byte)phase;
                    while(Stopwatch.GetTimestamp()<end) {
                        long now=Stopwatch.GetTimestamp();if(now<next){if(rate<100&&next-now>Stopwatch.Frequency/500)Thread.Sleep(1);else Thread.SpinWait(10);continue;}
                        Put64(packet,8,++sequence);Put64(packet,24,(ulong)now);
                        try{int n=s.Send(packet);rows[phase].Packets++;rows[phase].Bytes+=n;}catch(SocketException){rows[phase].Errors++;}
                        next+=step;now=Stopwatch.GetTimestamp();if(now-next>Stopwatch.Frequency*.003)next=now;
                    }
                    phaseStart=end;
                }
                Thread.Sleep(200);Volatile.Write(ref endRx,true);s.Close();rx.Join(1000);
            }
            lock(Gate)output.Sent.AddRange(rows);
        }
        static void ReadFull(Socket s,byte[] b){int p=0;while(p<b.Length){int n=s.Receive(b,p,b.Length-p,SocketFlags.None);if(n==0)throw new Exception("TCP EOF");p+=n;}}
        static void RunTCP(Case c,string local,Output output) {
            using(var s=new Socket(AddressFamily.InterNetwork,SocketType.Stream,ProtocolType.Tcp)) {
                s.NoDelay=true;s.ReceiveTimeout=1500;s.SendTimeout=1500;s.Bind(new IPEndPoint(IPAddress.Parse(local),c.SourcePort));
                var ar=s.BeginConnect(IPAddress.Parse(c.Address),c.Port,null,null);if(!ar.AsyncWaitHandle.WaitOne(3000))throw new TimeoutException("TCP connect");s.EndConnect(ar);
                for(int phase=0;phase<2;phase++){
                    var row=new Tx{Case=c.Id,Phase=phase};var b=new byte[c.Size];
                    for(int i=0;i<(phase==0?32:128);i++){
                        var packet=Packet(c,phase,(ulong)i+1);long now=Stopwatch.GetTimestamp();int off=0;
                        while(off<packet.Length)off+=s.Send(packet,off,packet.Length-off,SocketFlags.None);
                        row.Packets++;row.Bytes+=packet.Length;ReadFull(s,b);if(!Header(b,0,b.Length)||b[6]!=c.Id)throw new Exception("Bad TCP echo");row.Replies++;
                        row.RTTms.Add((Stopwatch.GetTimestamp()-now)*1000.0/Stopwatch.Frequency);Thread.Sleep(2);
                    }
                    lock(Gate)output.Sent.Add(row);
                }
            }
        }
        public static Output Run(string local,Case[] cases,ulong token) {
            Token=token;Stop=false;WireRows=new Dictionary<int,Wire>();var output=new Output();var index=new Dictionary<int,Case>();
            var deadline=new Thread(()=>{Thread.Sleep(130000);if(!Stop)Environment.Exit(124);});deadline.IsBackground=true;deadline.Start();
            foreach(var c in cases){if(c.Id<1||c.Id>250||c.Size<64||c.Size>1400||c.Seconds<=0||c.Seconds>3||c.RateMbps<=0||c.RateMbps>900)throw new ArgumentException("Unsafe case bounds");index.Add(c.Id,c);}
            // This observer supplies UDP TOS; TCP capture is handled separately.
            using(var raw=new Socket(AddressFamily.InterNetwork,SocketType.Raw,ProtocolType.IP)) {
                raw.Bind(new IPEndPoint(IPAddress.Parse(local),0));raw.ReceiveBufferSize=8<<20;raw.ReceiveTimeout=300;
                raw.IOControl(IOControlCode.ReceiveAll,new byte[]{1,0,0,0},null);
                var capture=new Thread(()=>Sniff(raw,index,output));capture.IsBackground=true;capture.Start();
                try {
                    var doneGroups=new HashSet<int>();
                    bool observing=true;
                    foreach(var c in cases){
                        if(c.Group>0&&!doneGroups.Add(c.Group))continue;
                        // Bulk main traffic is one-way; no reverse marking is
                        // asserted for these groups. Avoid cloning their high
                        // outgoing packet rate into the UDP observer.
                        if(c.Bulk&&observing){raw.IOControl(IOControlCode.ReceiveAll,new byte[]{0,0,0,0},null);observing=false;}
                        if(!c.Bulk&&!observing){raw.IOControl(IOControlCode.ReceiveAll,new byte[]{1,0,0,0},null);observing=true;}
                        var group=new List<Case>();foreach(var x in cases)if((c.Group>0&&x.Group==c.Group)||(c.Group==0&&x.Id==c.Id))group.Add(x);
                        long start=Stopwatch.GetTimestamp()+Stopwatch.Frequency/10;var workers=new List<Thread>();
                        foreach(var spec in group){Case x=spec;var t=new Thread(()=>{try{if(x.Protocol=="tcp")RunTCP(x,local,output);else RunUDP(x,local,output,start);}catch(Exception e){lock(Gate)output.Errors.Add(x.Name+": "+e.Message);}});t.IsBackground=true;workers.Add(t);t.Start();}
                        foreach(var t in workers)if(!t.Join(12000))throw new TimeoutException("Case deadline");
                    }
                } finally {Thread.Sleep(200);Stop=true;raw.Close();capture.Join(1000);}
            }
            output.Received.AddRange(WireRows.Values);return output;
        }
    }
}
