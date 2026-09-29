"""Exercise the bounded packet splitter alone under random ready and response
latency.

The fixture's splitter cuts each 32-bit INCR burst into packets of at most
four beats from their own start (16-byte packets) and at 4 KiB. The bench
sends bursts of 1 to 24 beats (the next burst only after the previous one's B,
as a source with one burst per direction does), with random up-face valid,
random down-face ready, random B latency and some packets answered with a
target error. Checks:

- every down beat in order: address, start address, length, first and last
  of its packet, data, strobes and ID;
- packets follow each other without waiting for their B, with at most four
  packets sent whose B has not been taken, and the run must see a packet start
  while an earlier one's B is still outstanding (else PIPELINE_UNCOVERED);
- one up-face B per burst, after all its packet Bs, with the merged result
  (a target error in any packet);
- valid and payload hold until their handshake on both down faces' outputs."""
import argparse
import json
import random
import subprocess
from pathlib import Path

from cross_hardware_sim import simulate

PACKET_BYTES = 16
SEEDS = (1, 2, 3)


def plan(seed):
    rng = random.Random(seed)
    beats, packets, bursts = [], [], []
    for k in range(40):
        n = rng.randint(1, 24)
        start = rng.randrange(0, 0x1000 - 4 * n, 4) + 0x1000 * rng.randrange(0, 15)
        first_beat = len(beats)
        first_packet = len(packets)
        i = 0
        while i < n:
            pstart = start + 4 * i
            end = min(pstart + PACKET_BYTES, (pstart // 4096 + 1) * 4096)
            count = min((end - pstart) // 4, n - i)
            code = 2 if rng.random() < 0.15 else 0
            packets.append({'final': i + count == n, 'code': code, 'burst': k})
            for j in range(count):
                beats.append({'address': pstart + 4 * j, 'burst_start': start, 'length': n - 1,
                              'burst_first': i + j == 0, 'burst_last': i + j == n - 1,
                              'pstart': pstart, 'plength': count - 1, 'pfirst': j == 0,
                              'plast': j == count - 1, 'data': rng.getrandbits(32),
                              'id': k % 4, 'burst': k})
            i += count
        merged = 2 if any(p['code'] for p in packets[first_packet:]) else 0
        bursts.append({'first_beat': first_beat, 'merged': merged})
    return beats, packets, bursts


BENCH = r'''
`timescale 1ns/1ps
module tb;
@DECL@
nocloom_axi4_packetizer_probe dut(@CONN@);
initial nl_clk = 0;
always #5 nl_clk = ~nl_clk;
localparam NB=@NB@, NP=@NP@, NK=@NK@;
reg [31:0] b_address[0:NB-1], b_start[0:NB-1], b_pstart[0:NB-1], b_data[0:NB-1];
reg [7:0] b_length[0:NB-1], b_plength[0:NB-1];
reg b_first[0:NB-1], b_last[0:NB-1], b_pfirst[0:NB-1], b_plast[0:NB-1];
reg [1:0] b_id[0:NB-1];
integer b_burst[0:NB-1];
reg p_final[0:NP-1];
reg [1:0] p_code[0:NP-1];
integer p_burst[0:NP-1];
integer k_first[0:NK-1];
reg [1:0] k_merged[0:NK-1];
integer seed, cycle, up_i, down_i, packet_sent, packet_answered, bursts_done, delay, holding;
integer overlaps;
reg [31:0] held_address, held_data;
task check;
  input condition;
  input [8*24-1:0] name;
  begin
    if(condition !== 1'b1) begin
      $display("ASSERT_FAIL:PACKETIZER.%0s cycle=%0d up=%0d down=%0d sent=%0d answered=%0d",
               name, cycle, up_i, down_i, packet_sent, packet_answered);
      $fatal(1);
    end
  end
endtask
initial begin
@TABLES@
end
// Up face: a burst's beats only after the previous burst's B.
always @(negedge nl_clk) if(nl_reset_n) begin
  if(!nl_up_wreq_valid && up_i<NB && b_burst[up_i]<=bursts_done && ($random(seed)&3)!=0) begin
    nl_up_wreq_valid=1; nl_up_wreq_address=b_address[up_i]; nl_up_wreq_start_address=b_start[up_i];
    nl_up_wreq_length=b_length[up_i]; nl_up_wreq_size=2; nl_up_wreq_burst=1; nl_up_wreq_cache=3;
    nl_up_wreq_write=1; nl_up_wreq_id=b_id[up_i]; nl_up_wreq_count=4; nl_up_wreq_first=b_first[up_i];
    nl_up_wreq_last=b_last[up_i]; nl_up_wreq_data=b_data[up_i]; nl_up_wreq_be=4'hf;
  end
  nl_down_wreq_ready=($random(seed)&1);
  nl_up_wrsp_ready=($random(seed)&3)!=0;
  // Packet B after a random delay, held until taken.
  if(!nl_down_wrsp_valid && packet_answered<packet_sent) begin
    if(delay==0) begin
      nl_down_wrsp_valid=1; nl_down_wrsp_result=p_code[packet_answered]; nl_down_wrsp_first=1;
      nl_down_wrsp_last=1; nl_down_wrsp_count=4; nl_down_wrsp_id=b_id[k_first[p_burst[packet_answered]]];
      delay=$random(seed)&7;
    end else delay=delay-1;
  end
end
always @(posedge nl_clk) if(nl_reset_n) begin
  cycle=cycle+1;
  // Down-face valid and payload hold until taken.
  if(holding) check(nl_down_wreq_valid===1 && nl_down_wreq_address===held_address &&
                    nl_down_wreq_data===held_data, "DOWN_HOLD");
  holding=nl_down_wreq_valid && !nl_down_wreq_ready;
  held_address=nl_down_wreq_address; held_data=nl_down_wreq_data;
  if(nl_up_wreq_valid && nl_up_wreq_ready) begin
    up_i=up_i+1; nl_up_wreq_valid<=0;
  end
  if(nl_down_wreq_valid && nl_down_wreq_ready) begin
    check(down_i<NB, "DOWN_EXTRA");
    if(b_pfirst[down_i] && packet_answered<packet_sent) overlaps=overlaps+1;
    check(nl_down_wreq_address===b_address[down_i] && nl_down_wreq_start_address===b_pstart[down_i] &&
          nl_down_wreq_length===b_plength[down_i] && nl_down_wreq_first===b_pfirst[down_i] &&
          nl_down_wreq_last===b_plast[down_i], "DOWN_PACKET");
    check(nl_down_wreq_data===b_data[down_i] && nl_down_wreq_be===4'hf && nl_down_wreq_id===b_id[down_i] &&
          nl_down_wreq_size===2 && nl_down_wreq_burst===1, "DOWN_BEAT");
    if(b_plast[down_i]) begin
      packet_sent=packet_sent+1;
      check(packet_sent-packet_answered<=4, "IN_FLIGHT");
    end
    down_i=down_i+1;
  end
  if(nl_down_wrsp_valid && nl_down_wrsp_ready) begin
    check(packet_answered<packet_sent, "B_EARLY");
    packet_answered=packet_answered+1;
    nl_down_wrsp_valid<=0;
  end
  if(nl_up_wrsp_valid && nl_up_wrsp_ready) begin
    check(bursts_done<NK && packet_answered>0 && p_final[packet_answered-1] &&
          p_burst[packet_answered-1]==bursts_done, "UP_B_EARLY");
    check(nl_up_wrsp_result===k_merged[bursts_done] && nl_up_wrsp_id===b_id[k_first[bursts_done]],
          "UP_B_RESULT");
    bursts_done=bursts_done+1;
  end
end
initial begin
@INIT@
  if(!$value$plusargs("seed=%d", seed)) seed=1;
  cycle=0; up_i=0; down_i=0; packet_sent=0; packet_answered=0; bursts_done=0; delay=0; holding=0;
  overlaps=0;
  nl_reset_n=0; repeat(3) @(posedge nl_clk); #1 nl_reset_n=1;
  wait(bursts_done==NK || cycle>200000);
  check(bursts_done==NK && down_i==NB && packet_answered==NP, "COMPLETION");
  check(overlaps>0, "PIPELINE_UNCOVERED");
  $display("AXI4_TARGET_PASS packetizer bursts=%0d packets=%0d beats=%0d cycles=%0d overlaps=%0d", NK, NP, NB, cycle, overlaps);
  $finish;
end
endmodule
'''


def write_bench(work, ports, seed):
    beats, packets, bursts = plan(seed)
    tables = []
    for i, b in enumerate(beats):
        tables.append(
            f"  b_address[{i}]=32'h{b['address']:x}; b_start[{i}]=32'h{b['burst_start']:x}; "
            f"b_pstart[{i}]=32'h{b['pstart']:x}; b_data[{i}]=32'h{b['data']:x}; "
            f"b_length[{i}]={b['length']}; b_plength[{i}]={b['plength']}; "
            f"b_first[{i}]={int(b['burst_first'])}; b_last[{i}]={int(b['burst_last'])}; "
            f"b_pfirst[{i}]={int(b['pfirst'])}; b_plast[{i}]={int(b['plast'])}; "
            f"b_id[{i}]={b['id']}; b_burst[{i}]={b['burst']};")
    for i, p in enumerate(packets):
        tables.append(f"  p_final[{i}]={int(p['final'])}; p_code[{i}]={p['code']}; "
                      f"p_burst[{i}]={p['burst']};")
    for i, k in enumerate(bursts):
        tables.append(f"  k_first[{i}]={k['first_beat']}; k_merged[{i}]={k['merged']};")
    text = BENCH
    for key, value in {
            'DECL': '\n'.join(f"{'reg' if i else 'wire'} [{w-1}:0] {n};" for n, w, i in ports),
            'CONN': ', '.join(f'.{n}({n})' for n, _, _ in ports),
            'INIT': '\n'.join(f'  {n}=0;' for n, w, i in ports if i and n != 'nl_clk'),
            'TABLES': '\n'.join(tables), 'NB': str(len(beats)), 'NP': str(len(packets)),
            'NK': str(len(bursts))}.items():
        text = text.replace(f'@{key}@', value)
    (work / 'tb.sv').write_text(text)
    return len(beats), len(packets)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture', required=True)
    parser.add_argument('--work', required=True)
    args = parser.parse_args()
    root = Path(args.work).resolve()
    results = []
    for seed in SEEDS:
        work = root / f'seed{seed}'
        work.mkdir(parents=True, exist_ok=True)
        subprocess.run([args.fixture, str(work)], check=True, timeout=120)
        ports = [(n, int(w), i == '1') for n, w, i in
                 (line.split() for line in (work / 'ports.tsv').read_text().splitlines())]
        nb, np_ = write_bench(work, ports, seed)
        rc = simulate(work, root / 'compiled', (f'+seed={seed}',))
        log = (work / 'simulation.log').read_text()
        passed = rc == 0 and 'AXI4_TARGET_PASS packetizer' in log and 'ASSERT_FAIL' not in log
        results.append({'seed': seed, 'beats': nb, 'packets': np_, 'returncode': rc,
                        'status': 'PASS' if passed else 'FAIL'})
        if not passed:
            (root / 'results.json').write_text(json.dumps(results, indent=1) + '\n')
            raise RuntimeError(f'Packetizer seed {seed} failed: {log[-2000:]}')
    (root / 'results.json').write_text(json.dumps(results, indent=1) + '\n')
    print('AXI4_TARGET_PASS packetizer_order seeds=%d' % len(results))


if __name__ == '__main__':
    main()
