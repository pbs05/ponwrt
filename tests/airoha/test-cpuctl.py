#!/usr/bin/env python3
"""Exercise the shipped ucode policy against an in-memory device. Never SSH."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / 'package/network/config/airoha-cpu/files'

PROGRAM = r'''
import { cpumask, cpulist, taskstat, parse_map, make_plan, apply, restore } from 'POLICY';
let assertions = 0;
function ok(v, text) { assertions++; if (!v) die(text); }
function throws(f, text) { let threw = false; try { f(); } catch(e) { threw = true; } ok(threw, text); }
function copy(v) { return json(sprintf('%J', v)); }
const cfg = { wan_cpu: '1', lan_cpu: '2', special_cpu: '3', other_irq_cpu: '0', service_cpus: '0,3', unbound_cpus: '0,3' };
let maptext = '# airoha-napi-map 1\n', tasks = {}, irqs = {};
for (let q = 0; q < 2; q++) {
    for (let b = 0; b < 4; b++) irqs[`${20+q*4+b}`] = {action:`airoha_eth.${q*4+b}`, mask:15};
    for (let r = 0; r < 34; r++) {
        let kind = r < 32 ? 'rx' : 'tx', queue = r < 32 ? r : r-32;
        let bank = r >= 32 ? 0 : r == 6 ? 3 : r == 5 ? 2 : r == 2 ? 1 : r == 31 ? -1 : 0;
        let pid = 100+q*34+r, irq = bank < 0 ? -1 : 20+q*4+bank;
        maptext += `${q} ${kind} ${queue} ${bank} ${irq} ${pid} 1024 16384 1024\n`;
        tasks[`${pid}`] = {name:'napi/qdma_eth-0', start:`${pid}`, mask:15, user:false};
    }
}
tasks['1'] = { name:'procd', start:'1', mask:15, user:true };
tasks['400'] = { name:'rtp2httpd', start:'50', mask:15, user:true };
tasks['401'] = { name:'explicit-custom', start:'51', mask:4, user:true };
tasks['402'] = { name:'kworker/1:0', start:'52', mask:2, user:false };
irqs['70'] = { action:'airoha-xpon-phy', mask:15, housekeeping:true };
let snap = {board:'nokia,xg-040g-md-ubi', online:15, rows:parse_map(maptext), tasks, irqs,
    queues:[{path:'/rps',kind:'mask',value:1},{path:'/rfs',kind:'count',value:1024}], default_irq:15, unbound:15};
ok(cpumask('0-3') == 15 && cpumask('0,3') == 9 && cpulist(9) == '0,3', 'CPU list round trip');
for (let list in ['4','0;reboot','3-1','',null]) throws(() => cpumask(list),'reject bad CPU list');
let stat = taskstat('42 (worker (test)) S 2 3 4 5 6 2097152 0 0 0 0 0 0 0 0 0 0 0 0 777 0\n');
ok(stat && stat.start == '777' && !stat.user && stat.name == 'worker (test)', 'stat parser identity');
throws(() => parse_map(replace(maptext,'# airoha-napi-map 1','# airoha-napi-map 2')),'map version');
throws(() => parse_map(maptext+'0 rx 0 0 20 100 1 1 1\n'),'duplicate map');
let p = make_plan(cfg,snap,'full');
let alternate=copy(snap);alternate.board='nokia,xg-040g-md-ubi-usb-sfp';
ok(make_plan(cfg,alternate,'network').stage=='network','actual USB/SFP compatible name');
ok(length(filter(p.ops,(o)=>o.kind == 'irq')) == 9,'eight DMA and one housekeeping IRQ');
ok(filter(p.ops,(o)=>o.id == 'irq:24')[0].want == 2,'WAN ordinary CPU1');
ok(filter(p.ops,(o)=>o.id == 'irq:20')[0].want == 4,'LAN ordinary CPU2');
ok(filter(p.ops,(o)=>o.id == 'irq:27')[0].want == 8,'ring6 special CPU3');
ok(!length(filter(p.ops,(o)=>o.tid == 401 || o.tid == 402)),'custom affinity and workers preserved');
ok(!length(filter(make_plan(cfg,snap,'network').ops,(o)=>o.role == 'service')),'network stage excludes userspace');
for (let edit in ['board','offline','thread','irq','overlap','queues','workqueue','init']) {
    let s=copy(snap), c=copy(cfg);
    if(edit=='board') s.board='other-board';
    if(edit=='offline') s.online=7;
    if(edit=='thread') delete s.tasks['106'];
    if(edit=='irq') s.irqs['24'].action='wrong';
    if(edit=='overlap') c.service_cpus='1,3';
    if(edit=='queues') s.queues=[];
    if(edit=='workqueue') s.unbound=null;
    if(edit=='init') s.tasks['1'].mask=1;
    throws(() => make_plan(c,s,'full'),`preflight ${edit}`);
}
function fixture() {
    let values={}, saved=null, writes=0, fail=null, effective_bad=false;
    for (let op in p.ops) values[op.id]=op.was;
    let state={ops:[],online:15,service_from:1000,service_mask:9,preexisting:{}};
    let io={
        tasks:()=>({}),
        save:(s)=>{saved=copy(s);},
        read:(o)=>values[o.id],
        write:(o,v)=>{ok(saved != null,'journal precedes write');writes++;if(o.id==fail)die('injected write failure');values[o.id]=v;},
        effective:(o)=>effective_bad ? 1 : values[o.id],
    };
    return {io,state,values,fail:(s)=>{fail=s;},bad_effective:()=>{effective_bad=true;}};
}
let f=fixture();apply(f.io,p,f.state);
ok(f.state.active && f.values['irq:24']==2 && f.values['task:400:50']==9,'apply');
ok(restore(f.io,f.state).restored && f.values['irq:24']==15 && f.values['task:400:50']==15,'restore baseline');
f=fixture();f.fail('irq:24');throws(()=>apply(f.io,p,f.state),'partial write failure');
ok(f.values['irq:20']==15 && !f.state.active && f.state.restored,'rollback after partial apply');
f=fixture();f.bad_effective();throws(()=>apply(f.io,p,f.state),'effective mismatch');
ok(f.values['irq:20']==15,'rollback on IRQ hardware disagreement');
f=fixture();apply(f.io,p,f.state);f.values['task:400:50']=null;
ok(restore(f.io,f.state).restored,'exited/reused PID skipped');
f=fixture();apply(f.io,p,f.state);f.values['task:400:50']=1;
ok(length(restore(f.io,f.state).conflicts)==1 && f.values['task:400:50']==1,'external affinity not overwritten');
f=fixture();apply(f.io,p,f.state);f.fail('irq:24');
ok(!restore(f.io,f.state).restored,'failed restore retained');f.fail(null);
ok(restore(f.io,f.state).restored,'restore retry');
f=fixture();apply(f.io,p,f.state);
f.io.tasks=()=>({'500':{start:'1001',user:true,mask:9},'501':{start:'1002',user:true,mask:1}});
f.values['task:500:1001']=9;f.values['task:501:1002']=1;
restore(f.io,f.state);
ok(f.values['task:500:1001']==15 && f.values['task:501:1002']==1,'inherited children restored, custom child untouched');
printf('PASS: %d policy/transaction assertions\n', assertions);
'''


def main():
    a = argparse.ArgumentParser()
    a.add_argument('--ucode', default='ucode')
    a.add_argument('--module-dir')
    args = a.parse_args()
    cmd = [args.ucode] + (['-L', args.module_dir] if args.module_dir else [])
    with tempfile.TemporaryDirectory(prefix='hg-cpuctl-') as directory:
        root = Path(directory)
        program = root/'test.uc'
        program.write_text(PROGRAM.replace('POLICY', str(PACKAGE/'policy.uc')))
        subprocess.run(cmd+[str(program)], check=True)
        # Compile the real frontend with stub UCI and its real policy module.
        # No real /proc or /sys access takes place during compilation.
        (root/'uci.uc').write_text('export function cursor() { return {}; }\n')
        front = root/'front.uc'
        front.write_text((PACKAGE/'airoha-cpuctl').read_text().replace(
            '/usr/libexec/airoha-cpu/policy.uc', str(PACKAGE/'policy.uc')))
        subprocess.run(cmd+['-L',str(root),'-c','-o',str(root/'front.ucb'),str(front)],check=True)
        # Run real lease/ownership command dispatch with a fully fake fs module.
        # This checks watchdog expiry without reading or writing host /proc.
        stub = '''export function cursor() { return { get_all:()=>({enabled:'0'}) }; }\n'''
        (root/'uci.uc').write_text(stub)
        front.write_text(front.read_text().replace("from 'fs'", "from '"+str(root/'fs.uc')+"'"))
        cases = [
            (['trial','network','180','invalid-token'],dict(active=False,restored=True),1,'Optional trial token must be a UUID'),
            (['watch','token'],dict(active=True,restored=False),10,'"restored": true'),
            (['watch','token'],dict(active=False,restored=False),10,'"restored": true'),
            (['renew','token'],dict(active=True,restored=False),1,'Trial already expired'),
            (['restore','other-token'],dict(active=True,restored=False),1,'Restore token does not own'),
            (['sync'],dict(active=False,restored=False),1,'Previous rollback is incomplete'),
            (['sync'],dict(active=False,restored=True),10,''),
        ]
        for argv, flags, code, expected in cases:
            state=dict(version=1,boot='boot',trial=True,token='token',ops=[],deadline=0,lease=180,**flags)
            values={'/tmp/airoha-cpu/state.json':json.dumps(state),'/proc/sys/kernel/random/boot_id':'boot'}
            (root/'fs.uc').write_text('let data='+json.dumps(values)+';\n'+r'''
export function readfile(p) { return data[p]; }
export function writefile(p,v) { data[p]=v; return length(v); }
export function rename(a,b) { data[b]=data[a]; return true; }
export function mkdir(p,m) { return true; }
export function chmod(p,m) { return true; }
export function open(p,m,n) { return {lock:()=>true}; }
export function glob(p) { return []; }
''')
            r=subprocess.run(cmd+['-L',str(root),str(front),*argv],capture_output=True,text=True)
            assert r.returncode==code,(argv,r.stdout,r.stderr)
            assert expected in r.stdout+r.stderr,(argv,r.stdout,r.stderr)
        ctl=root/'ctl'; calls=root/'calls'
        ctl.write_text('#!/bin/sh\nif [ -e "'+str(calls)+'" ]; then echo done >> "'+str(calls)+'"; exit 10; fi\necho retry > "'+str(calls)+'"\nexit 1\n')
        ctl.chmod(0o700)
        guard=root/'guard'
        guard.write_text((PACKAGE/'guard').read_text().replace('/usr/sbin/airoha-cpuctl',str(ctl))
                         .replace('/tmp/airoha-cpu',str(root)).replace('sleep 2','sleep 0.01'))
        subprocess.run(['sh',str(guard),'abcdef01-0000-0000-0000-abcdef012345'],check=True)
        for _ in range(100):
            if calls.exists() and 'done' in calls.read_text():break
            time.sleep(.01)
        assert calls.read_text().splitlines()==['retry','done'],'watchdog must retry errors and stop only on completion'
    for filename in ('guard','airoha-cpu.init','packet-steering.sh'):
        subprocess.run(['sh','-n',str(PACKAGE/filename)],check=True)
    print('PASS: frontend compilation, seven lease/ownership cases and shell syntax')


if __name__ == '__main__':
    main()
