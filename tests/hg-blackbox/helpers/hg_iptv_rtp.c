/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * Ordinary UDP membership: no AF_PACKET, promiscuity, decoding or forwarding.
 * Arguments: rtp IFACE GROUP PORT SOURCE SOURCE_PORT SECONDS WARMUP [FIRST_TIMEOUT].
 * Counts cover the measured interval; warmup/total socket drops are separate.
 * Join and first valid packet timestamps use the same host monotonic clock.
 * A sequence jump is an event, not necessarily missing data:
 * extend the 16-bit sequence about its high-water mark and count unique values
 * separately. SSRC changes / table overflow make this inference inconclusive.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <net/if.h>
#include <poll.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static double now(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec+t.tv_nsec/1e9;}
static void hist(unsigned long *a,int n){int comma=0;putchar('{');for(int i=0;i<n;i++)if(a[i]){printf("%s\"%d\":%lu",comma?",":"",i,a[i]);comma=1;}putchar('}');}
/* Bounded bitset: 4M sequence positions with 512 KiB observer memory. */
static unsigned char sequence_seen[512*1024];
static struct {double seconds; unsigned previous,current; int delta,ttl;} events[128];
static long number(const char *value){char *end;errno=0;long n=strtol(value,&end,10);return errno||!*value||*end?-1:n;}
int rtp_main(int argc,char**argv){
 if(argc!=8&&argc!=9)return 2;
 long first_timeout=argc==9?number(argv[8]):0;
 if(first_timeout<0||first_timeout>30)return 2;
 unsigned index=if_nametoindex(argv[1]);long port=number(argv[3]),sport=number(argv[5]),seconds=number(argv[6]),warmup=number(argv[7]);
 if(!index||port<1024||port>65535||sport<1||sport>65535||seconds<1||seconds>300||warmup<0||warmup>30)return 2;
 struct in_addr group,expected_source;if(inet_pton(AF_INET,argv[2],&group)!=1||!IN_MULTICAST(ntohl(group.s_addr))||inet_pton(AF_INET,argv[4],&expected_source)!=1)return 2;
 int fd=socket(AF_INET,SOCK_DGRAM|SOCK_CLOEXEC,0),one=1,zero=0,buffer=4*1024*1024;
 struct ip_mreqn req={.imr_multiaddr=group,.imr_ifindex=index};
 struct sockaddr_in address={.sin_family=AF_INET,.sin_port=htons(port),.sin_addr=group};
 if(fd<0||setsockopt(fd,SOL_SOCKET,SO_REUSEADDR,&one,sizeof(one))||setsockopt(fd,SOL_SOCKET,SO_RCVBUF,&buffer,sizeof(buffer))||setsockopt(fd,SOL_SOCKET,SO_RXQ_OVFL,&one,sizeof(one))||setsockopt(fd,IPPROTO_IP,IP_MULTICAST_ALL,&zero,sizeof(zero))||setsockopt(fd,IPPROTO_IP,IP_RECVTOS,&one,sizeof(one))||setsockopt(fd,IPPROTO_IP,IP_RECVTTL,&one,sizeof(one))||setsockopt(fd,IPPROTO_IP,IP_PKTINFO,&one,sizeof(one))||bind(fd,(void*)&address,sizeof(address))||setsockopt(fd,IPPROTO_IP,IP_ADD_MEMBERSHIP,&req,sizeof(req))){perror("probe socket");return 1;}
 double start=now();
 int actual_buffer=0;socklen_t buffer_len=sizeof(actual_buffer);struct stat socket_stat;
 if(getsockopt(fd,SOL_SOCKET,SO_RCVBUF,&actual_buffer,&buffer_len)||fstat(fd,&socket_stat)){perror("probe socket status");close(fd);return 1;}
 double begin=start+warmup,end=begin+seconds,first=-1;
 fprintf(stderr,"RTP_READY %.6f %.6f %.6f\n",start,begin,end);fflush(stderr);
 unsigned long packets=0,bytes=0,rtp=0,gaps=0,duplicates=0,nonrtp=0,invalid=0,ttl_h[256]={0},tos_h[256]={0},sport_h[65536]={0};
 uint16_t prev=0;uint32_t ssrc=0;bool seen=false;unsigned drops=0,warmup_drops=0;unsigned long warmup_packets=0;char source[64]="";double last=begin,maxgap=0;
 long seq_origin=0,seq_min=0,seq_high=0,seq_unique=0,seq_duplicate=0,seq_reordered=0,seq_overflow=0,ssrc_changes=0;
 unsigned event_count=0,source_mismatch=0;
 while(now()<end){if(first_timeout&&first<0&&now()-start>=first_timeout)break;struct pollfd p={.fd=fd,.events=POLLIN};int ready=poll(&p,1,100);if(ready<0&&errno!=EINTR)break;if(ready<=0)continue;
  unsigned char data[9000];_Alignas(struct cmsghdr) unsigned char control[256];struct sockaddr_in from;struct iovec iov={.iov_base=data,.iov_len=sizeof(data)};
  struct msghdr msg={.msg_name=&from,.msg_namelen=sizeof(from),.msg_iov=&iov,.msg_iovlen=1,.msg_control=control,.msg_controllen=sizeof(control)};
  int n=recvmsg(fd,&msg,0);double t=now();if(t>=end)break;if(n<0)continue;
  int ttl=-1,tos=-1,gotindex=0;struct in_addr dst={0};
  for(struct cmsghdr*c=CMSG_FIRSTHDR(&msg);c;c=CMSG_NXTHDR(&msg,c)){
   if(c->cmsg_level==IPPROTO_IP){
    if(c->cmsg_type==IP_TTL&&c->cmsg_len>=CMSG_LEN(sizeof(int)))memcpy(&ttl,CMSG_DATA(c),sizeof(int));
    if(c->cmsg_type==IP_TOS&&c->cmsg_len>=CMSG_LEN(1))tos=*(unsigned char*)CMSG_DATA(c);
    if(c->cmsg_type==IP_PKTINFO&&c->cmsg_len>=CMSG_LEN(sizeof(struct in_pktinfo))){struct in_pktinfo pi;memcpy(&pi,CMSG_DATA(c),sizeof(pi));gotindex=pi.ipi_ifindex;dst=pi.ipi_addr;}
   }else if(c->cmsg_level==SOL_SOCKET&&c->cmsg_type==SO_RXQ_OVFL&&c->cmsg_len>=CMSG_LEN(sizeof(drops)))memcpy(&drops,CMSG_DATA(c),sizeof(drops));
  }
  bool valid=ttl>=0&&ttl<=255&&tos>=0&&tos<=255&&gotindex==(int)index&&dst.s_addr==group.s_addr&&from.sin_addr.s_addr==expected_source.s_addr&&ntohs(from.sin_port)==sport&&!(msg.msg_flags&(MSG_TRUNC|MSG_CTRUNC));
  if(first<0&&valid&&n>=12&&(data[0]>>6)==2){
   first=t-start;
   /* Lifecycle runs measure a full receive window after the first packet.
    * Do not count normal IGMP join latency as a playback interruption. */
   if(first_timeout){begin=t+warmup;end=begin+seconds;last=begin;}
  }
  if(first_timeout&&first<0)continue;
  if(t<begin){warmup_packets++;warmup_drops=drops;continue;}
  packets++;bytes+=n;if(t-last>maxgap)maxgap=t-last;last=t;
  if(ttl<0||ttl>255||tos<0||tos>255||gotindex!=(int)index||dst.s_addr!=group.s_addr||from.sin_addr.s_addr!=expected_source.s_addr||ntohs(from.sin_port)!=sport||(msg.msg_flags&(MSG_TRUNC|MSG_CTRUNC)))invalid++;
  else{ttl_h[ttl]++;tos_h[tos]++;}
  sport_h[ntohs(from.sin_port)]++;inet_ntop(AF_INET,&from.sin_addr,source,sizeof(source));
  if(from.sin_addr.s_addr!=expected_source.s_addr||ntohs(from.sin_port)!=sport)source_mismatch++;
  if(n>=12&&(data[0]>>6)==2){uint16_t seq=(data[2]<<8)|data[3];uint32_t cur;memcpy(&cur,data+8,4);rtp++;
   if(seen&&cur==ssrc){if(seq==prev)duplicates++;else if(seq!=(uint16_t)(prev+1))gaps++;
    if(seq!=(uint16_t)(prev+1)&&event_count<128){events[event_count].seconds=t-begin;events[event_count].previous=prev;events[event_count].current=seq;events[event_count].delta=(int16_t)(seq-prev);events[event_count].ttl=ttl;event_count++;}}
   if(seen&&cur!=ssrc)ssrc_changes++;
   if(!seen){seq_min=seq_high=seq;seq_origin=(long)seq-32768;}
   long ext=seq_high+(int16_t)(seq-(uint16_t)seq_high),position=ext-seq_origin;
   if(ext<seq_high)seq_reordered++;
   if(ext<seq_min)seq_min=ext;
   if(ext>seq_high)seq_high=ext;
   if(position<0||position>=(long)sizeof(sequence_seen)*8)seq_overflow++;
   else if(sequence_seen[position/8]&(1U<<(position%8)))seq_duplicate++;
   else{sequence_seen[position/8]|=1U<<(position%8);seq_unique++;}
   prev=seq;ssrc=cur;seen=true;
  }else nonrtp++;
 }
 if(end-last>maxgap)maxgap=end-last;
 close(fd);
 printf("{\"ifindex\":%u,\"source\":\"%s\",\"seconds\":%.3f,\"packets\":%lu,\"bytes\":%lu,\"rtp_packets\":%lu,\"rtp_gap_events\":%lu,\"rtp_duplicates\":%lu,\"non_rtp\":%lu,\"invalid_metadata\":%lu,\"socket_drops\":%u,\"max_gap_seconds\":%.6f,\"ttl\":",index,source,now()>begin?now()-begin:0,packets,bytes,rtp,gaps,duplicates,nonrtp,invalid,drops-warmup_drops,maxgap);
 hist(ttl_h,256);printf(",\"tos\":");hist(tos_h,256);printf(",\"source_ports\":");hist(sport_h,65536);
 printf(",\"source_mismatch\":%u",source_mismatch);
 printf(",\"sequence_unique\":%ld,\"sequence_missing\":%ld,\"sequence_duplicates\":%ld,\"sequence_reordered\":%ld,\"sequence_overflow\":%ld,\"ssrc_changes\":%ld,\"events\":[",seq_unique,seen?seq_high-seq_min+1-seq_unique:0,seq_duplicate,seq_reordered,seq_overflow,ssrc_changes);
 for(unsigned i=0;i<event_count;i++)printf("%s{\"seconds\":%.6f,\"previous\":%u,\"current\":%u,\"delta\":%d,\"ttl\":%d}",i?",":"",events[i].seconds,events[i].previous,events[i].current,events[i].delta,events[i].ttl);
 printf("],\"socket_drops_total\":%u,\"warmup_socket_drops\":%u,\"warmup_packets\":%lu,\"rcvbuf_bytes\":%d,\"socket_inode\":%llu,\"join_monotonic_seconds\":%.9f,\"first_packet_delay_seconds\":",drops,warmup_drops,warmup_packets,actual_buffer,(unsigned long long)socket_stat.st_ino,start);
 if(first<0)printf("null");else printf("%.9f",first);
 puts("}");
 return packets?0:1;
}
