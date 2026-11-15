// The shared tape driver supplies classic and pipelined Wishbone requests.
module nlv_wishbone_master #(
    parameter NAME = "m", parameter ADDR_W = 32, parameter DATA_W = 32,
    parameter WORD = 0, parameter PIPELINED = 0, parameter REGISTERED_FEEDBACK = 0
) (
    input wire clk, rst_n,
    output wire cyc, stb, we,
    output wire [ADDR_W-1:0] adr,
    output wire [31:0] dat_w,
    output wire [3:0] sel,
    output wire [2:0] cti,
    output wire [1:0] bte,
    input wire [31:0] dat_r,
    input wire ack, err, stall
);
  wire [31:0] address;
  wire [3:0] mask;
  wire [2:0] prot;
  reg [3:0] read_sel = 0;
  reg abort_hold = 0, response_abort = 0;
  integer abort_response = 0, response_aborts = 0;
  wire driver_cyc, driver_stb;
  assign cyc = driver_cyc && !(response_abort && abort_response == 1);
  assign stb = driver_stb && !response_abort;
  // Withdraw activity between the registered response and its sampling edge.
  always @(negedge clk) begin
    if (!rst_n || abort_response == 0) response_abort = 0;
    else if (!response_abort && driver_cyc && (ack || err)) begin
      response_abort = 1;
      response_aborts = response_aborts + 1;
    end
  end
  nlv_apb_master #(.NAME(NAME), .ADDR_W(32), .DATA_W(32), .CLASSIC(1), .PIPELINED(PIPELINED)) driver (
      .clk(clk), .rst_n(rst_n && !abort_hold && !response_abort), .psel(driver_cyc), .penable(driver_stb), .pwrite(we),
      .paddr(address), .pwdata(dat_w), .pstrb(mask), .pprot(prot),
      .stall(stall), .prdata(dat_r), .pready(ack | err), .pslverr(err));
  assign adr = WORD ? address >> 2 : address;
  assign sel = we ? mask : read_sel;
  reg [2:0] drive_cti = 0;
  reg [1:0] drive_bte = 0;
  assign cti = REGISTERED_FEEDBACK ? drive_cti : 0;
  assign bte = REGISTERED_FEEDBACK ? drive_bte : 0;
  nlv_wishbone_check #(.NAME(NAME), .ADDR_W(ADDR_W), .ALLOW_ABORT(1), .PIPELINED(PIPELINED),
      .REGISTERED_FEEDBACK(REGISTERED_FEEDBACK), .WORD(WORD),
      .MASTER(0)) wb_check (
      .cti(cti), .bte(bte), .stall(stall), .clk(clk), .rst_n(rst_n), .cyc(cyc), .stb(stb), .we(we), .adr(adr),
      .dat_w(dat_w), .dat_r(dat_r), .sel(sel), .ack(ack), .err(err), .rty(1'b0), .constant_ack(1'b0));
  function [63:0] counter(input integer index);
    counter = index == 23 ? response_aborts : driver.counter(index);
  endfunction
  task set_ctl(input integer index, input [31:0] value);
    if (index == 74) drive_cti = value;
    else if (index == 75) drive_bte = value;
    else if (index == 70) read_sel = value;
    else if (index == 72) abort_hold = value != 0;
    // The tape and the response abort logic both run on the falling edge.
    else if (index == 73) abort_response <= value;
    else driver.set_ctl(index, value);
  endtask
  task push_write(input [63:0] a, input [7:0] n, input [2:0] s, input [1:0] b,
      input [31:0] id, input lk, input [3:0] ca, input [2:0] pr,
      input [3:0] qo, input [7:0] m, input [31:0] salt, tag);
    driver.push_write(a, n, s, b, id, lk, ca, 3'd2, qo, m, salt, tag);
  endtask
  task push_read(input [63:0] a, input [7:0] n, input [2:0] s, input [1:0] b,
      input [31:0] id, input lk, input [3:0] ca, input [2:0] pr,
      input [3:0] qo, input [31:0] tag);
    if (s == 0) read_sel = 1 << a[1:0];
    if (s == 1) read_sel = 3 << (a[1:0] & 2);
    driver.push_read(a, n, s, b, id, lk, ca, 3'd2, qo, tag);
  endtask
endmodule
