`timescale 1ns/1ps
// TileLink 1.8.1 single-beat peer with independent protocol checks.
module nlv_tl_slave #(parameter NAME="tl", ADDR_W=32, DATA_W=32, ID_W=4, SIZE_W=2, SINK_W=1, MEM_BYTES=65536, SALT=0)(
    input wire clk, input wire rst_n,
    input wire [3-1:0] a_opcode,
    input wire [3-1:0] a_param,
    input wire [SIZE_W-1:0] a_size,
    input wire [ID_W-1:0] a_source,
    input wire [ADDR_W-1:0] a_address,
    input wire [4-1:0] a_mask,
    input wire [32-1:0] a_data,
    input wire a_corrupt,
    input wire a_valid,
    output reg a_ready,
    output reg [3-1:0] d_opcode,
    output reg [2-1:0] d_param,
    output reg [SIZE_W-1:0] d_size,
    output reg [ID_W-1:0] d_source,
    output reg [SINK_W-1:0] d_sink,
    output reg [32-1:0] d_data,
    output reg d_corrupt,
    output reg d_denied,
    output reg d_valid,
    input wire d_ready,
    input wire sideband
);
  wire x_hsel;
  wire [ADDR_W-1:0] x_haddr;
  wire [2-1:0] x_htrans;
  wire x_hwrite;
  wire [3-1:0] x_hsize;
  wire [3-1:0] x_hburst;
  wire [4-1:0] x_hprot;
  wire x_hmastlock;
  wire [DATA_W-1:0] x_hwdata;
  wire x_hready;
  wire x_hreadyout;
  wire x_hresp;
  wire [DATA_W-1:0] x_hrdata;
  // TileLink 1.8.1 sections 4.5 and 7.2.3: only selected bytes are written.
  reg [3:0] mask=0;
  nlv_ahb_slave #(.NAME(NAME), .ADDR_W(ADDR_W), .DATA_W(32), .MEM_BYTES(MEM_BYTES),
                  .SALT(SALT), .MASKED_WRITES(1)) peer (
    .clk(clk), .rst_n(rst_n), .sideband(sideband),
    .hsel(x_hsel),
    .haddr(x_haddr),
    .htrans(x_htrans),
    .hwrite(x_hwrite),
    .hsize(x_hsize),
    .hburst(x_hburst),
    .hprot(x_hprot),
    .hmastlock(x_hmastlock),
    .hwdata(x_hwdata),
    .hready(x_hready),
    .hreadyout(x_hreadyout),
    .hresp(x_hresp),
    .hrdata(x_hrdata), .write_mask(mask)
  );
  integer corrupt_only=0, immediate_error=0, same_cycle=0, a_stalls=0, a_waited=0;
  function [63:0] counter(input integer index); counter = index==21 ? same_cycle : index==22 ? a_stalls : peer.counter(index); endfunction
  task set_ctl(input integer index, input [31:0] value); if (index==77) corrupt_only=value; else if (index==78) immediate_error=value; else peer.set_ctl(index, value); endtask
  reg [2:0] phase=0;
  reg [ADDR_W-1:0] addr=0;
  reg [2:0] size=0;
  reg wr=0;
  reg [31:0] data=0;
  reg [ID_W-1:0] source=0;
  assign x_hsel = phase == 1;
  assign x_htrans = phase == 1 ? 2 : 0;
  assign x_haddr = addr;
  assign x_hsize = size;
  assign x_hwrite = wr;
  assign x_hwdata = data;
  assign x_hburst = 0;
  assign x_hprot = 1;
  assign x_hmastlock = 0;
  assign x_hready = x_hreadyout;
  always @* a_ready = rst_n && phase == 0 && a_waited>=2;
  reg stored_d_valid;
  reg [2:0] stored_d_opcode;
  reg [1:0] stored_d_param;
  reg [SIZE_W-1:0] stored_d_size;
  reg [ID_W-1:0] stored_d_source;
  reg [SINK_W-1:0] stored_d_sink;
  reg [31:0] stored_d_data;
  reg stored_d_denied;
  reg stored_d_corrupt;
  initial begin stored_d_valid=0; stored_d_opcode=0; stored_d_param=0; stored_d_size=0; stored_d_source=0;
    stored_d_sink=0; stored_d_data=0; stored_d_denied=0; stored_d_corrupt=0; end
  always @(posedge clk) begin
    if (!rst_n) begin phase<=0; stored_d_valid<=0; a_waited<=0; end
    else begin
      if (a_valid && !a_ready && phase==0) begin a_waited<=a_waited+1; a_stalls<=a_stalls+1; end
      if (a_valid && a_ready) a_waited<=0;
      if (a_valid && a_ready && !immediate_error) begin
        addr<=a_address; size<=a_size; source<=a_source; wr<=a_opcode != 4;
        data<=a_data; mask<=a_mask; phase<=1;
      end
      if (phase==1 && x_hreadyout) phase<=2;
      if (phase==2 && x_hreadyout) begin
        stored_d_valid<=1; stored_d_opcode<=wr ? 0 : 1; stored_d_size<=size; stored_d_source<=source;
        stored_d_denied<=x_hresp && (wr || !corrupt_only); stored_d_corrupt<=!wr && x_hresp; stored_d_data<=x_hrdata; phase<=3;
      end
      if (stored_d_valid && d_ready) begin stored_d_valid<=0; phase<=0; end
    end
  end
  always @* begin
    d_valid=immediate_error ? (a_valid && a_ready) : stored_d_valid;
    d_opcode=immediate_error ? (a_opcode==4 ? 1 : 0) : stored_d_opcode;
    d_param=immediate_error ? 0 : stored_d_param;
    d_size=immediate_error ? a_size : stored_d_size;
    d_source=immediate_error ? a_source : stored_d_source;
    d_sink=immediate_error ? 0 : stored_d_sink;
    d_data=immediate_error ? 0 : stored_d_data;
    d_denied=immediate_error ? 1 : stored_d_denied;
    d_corrupt=immediate_error ? (a_opcode==4) : stored_d_corrupt;
  end
  always @(posedge clk) begin
    if (rst_n && immediate_error && a_valid && a_ready) begin
      same_cycle=same_cycle+1;
      $fwrite(tb.nlv_fd,
        "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"ahb_addr\",\"a\":\"%0h\",\"w\":%0d,\"sz\":%0d,\"bu\":0,\"tr\":2,\"pr\":1,\"lk\":0}\n",
        ($realtime*1000.0), peer.cyc, NAME, a_address, a_opcode!=4, a_size);
      $fwrite(tb.nlv_fd,
        "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"ahb_data\",\"a\":\"%0h\",\"w\":%0d,\"d\":\"%0h\",\"r\":1,\"waits\":0,\"s\":\"%0h\"}\n",
        ($realtime*1000.0), peer.cyc, NAME, a_address, a_opcode!=4, a_opcode==4 ? 32'd0 : a_data, a_mask);
    end
  end
  nlv_tl_checker #(.NAME(NAME), .ADDR_W(ADDR_W), .SOURCE_W(ID_W), .SIZE_W(SIZE_W)) check (
    .clk(clk), .rst_n(rst_n), .allow_reject(1'b0),
    .a_opcode(a_opcode),
    .a_param(a_param),
    .a_size(a_size),
    .a_source(a_source),
    .a_address(a_address),
    .a_mask(a_mask),
    .a_data(a_data),
    .a_corrupt(a_corrupt),
    .a_valid(a_valid),
    .a_ready(a_ready),
    .d_opcode(d_opcode),
    .d_param(d_param),
    .d_size(d_size),
    .d_source(d_source),
    .d_data(d_data),
    .d_corrupt(d_corrupt),
    .d_denied(d_denied),
    .d_valid(d_valid),
    .d_ready(d_ready)
  );
endmodule
