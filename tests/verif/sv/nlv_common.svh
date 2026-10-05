// Shared helpers for the tests/verif BFMs. Included inside module bodies.
// Portability: plain Verilog-2001 constructs plus $value$plusargs and file
// I/O; no class, interface, covergroup or UVM. Time-stepped BFM logic runs
// in clocked always blocks only, so no task waits on an event.

// Byte address of beat i of an AXI burst (A3.4.1).
function [63:0] nlv_beat_addr;
  input [63:0] start;
  input [7:0] len;
  input [2:0] size;
  input [1:0] burst;
  input integer i;
  reg [63:0] bytes, aligned, total, base;
  begin
    bytes = 64'd1 << size;
    aligned = start & ~(bytes - 1);
    if (burst == 2'd0 || i == 0)
      nlv_beat_addr = start;
    else if (burst == 2'd2) begin
      total = bytes * ({56'd0, len} + 64'd1);
      base = start & ~(total - 1);
      nlv_beat_addr = base + ((aligned - base + bytes * i) % total);
    end else
      nlv_beat_addr = aligned + bytes * i;
  end
endfunction

// Active byte lanes of a beat on a bus of nbus bytes: [lo, hi].
function [15:0] nlv_lane_lo;
  input [63:0] addr;
  input integer nbus;
  begin
    nlv_lane_lo = addr % nbus;
  end
endfunction

function [15:0] nlv_lane_hi;
  input [63:0] addr;
  input [2:0] size;
  input integer nbus;
  reg [63:0] bytes;
  begin
    bytes = 64'd1 << size;
    nlv_lane_hi = ((addr & ~(bytes - 1)) % nbus) + bytes - 1;
  end
endfunction

// Deterministic stimulus byte for a source write; the checker never assumes
// this function, it reads the observed bytes from the event log.
function [7:0] nlv_source_byte;
  input [63:0] addr;
  input [31:0] salt;
  begin
    nlv_source_byte = ((addr * 7) + (salt * 31) + (addr >> 8) * 3) ^ 8'ha5;
  end
endfunction

// Initial target memory byte; mirrored by nlverif.model.initial_byte().
function [7:0] nlv_target_byte;
  input [63:0] addr;
  input [31:0] salt;
  begin
    nlv_target_byte = ((addr * 13) + (addr >> 8) * 7 + salt * 57) ^ 8'h5a;
  end
endfunction

// Ready pattern: mode 0 high, 1 low, 2 high after valid waited a1 cycles,
// 3 high unless cycle % a1 == a2, 4 pseudo-random with a1 percent high.
function nlv_ready_pattern;
  input [7:0] mode;
  input [31:0] a1, a2;
  input [63:0] cycle;
  input [31:0] waited;
  input [31:0] lfsr;
  begin
    case (mode)
      8'd0: nlv_ready_pattern = 1'b1;
      8'd1: nlv_ready_pattern = 1'b0;
      8'd2: nlv_ready_pattern = waited >= a1;
      8'd3: nlv_ready_pattern = a1 == 0 || (cycle % a1) != a2;
      8'd4: nlv_ready_pattern = (lfsr % 100) < a1;
      default: nlv_ready_pattern = 1'b1;
    endcase
  end
endfunction

function [31:0] nlv_lfsr_next;
  input [31:0] value;
  begin
    nlv_lfsr_next = {value[30:0], value[31] ^ value[21] ^ value[1] ^ value[0]};
    if (nlv_lfsr_next == 0)
      nlv_lfsr_next = 32'h1;
  end
endfunction

function integer nlv_compare;
  input [63:0] value;
  input [7:0] cmp;
  input [63:0] bound;
  begin
    case (cmp)
      8'd0: nlv_compare = value == bound;
      8'd1: nlv_compare = value != bound;
      8'd2: nlv_compare = value >= bound;
      8'd3: nlv_compare = value <= bound;
      8'd4: nlv_compare = value > bound;
      8'd5: nlv_compare = value < bound;
      default: nlv_compare = 0;
    endcase
  end
endfunction
