`timescale 1ns/1ps
module tb;
  localparam integer COUNT = SELECTOR_COUNT;
  localparam bit INDEPENDENT = INDEPENDENT_WINDOWS;
  localparam [31:0] LAST = 32'h1 << (COUNT - 1);
  localparam [31:0] ALL = 32'hffffffff >> (32 - COUNT);
  localparam [19:0] LAST_PAGE = ((COUNT + 3) / 4) * 4096;
  reg clk = 0;
  always #5 clk = ~clk;

  reg reset_n = 0;
  reg [31:0] modes = 0;
  reg sel = 0, enable = 0, write = 0;
  reg [19:0] address = 0;
  reg [31:0] data = 0;
  wire ready, error;
  wire [31:0] answer;
  wire ram_sel, ram_enable, ram_write;
  wire [19:0] ram_address;
  wire [31:0] ram_data;
  wire [3:0] ram_strobe;
  nocloom_selectors dut(.clk(clk), .reset_n(reset_n), .test(1'b0),
    SELECTOR_INPUTS
    .nl_i_cpu_psel(sel), .nl_i_cpu_penable(enable), .nl_i_cpu_pwrite(write),
    .nl_i_cpu_paddr(address), .nl_i_cpu_pwdata(data), .nl_i_cpu_pstrb(4'hf),
    .nl_i_cpu_pprot(3'b0), .nl_i_cpu_pready(ready), .nl_i_cpu_prdata(answer),
    .nl_i_cpu_pslverr(error), .nl_i_cfg_psel(1'b0), .nl_i_cfg_penable(1'b0),
    .nl_i_cfg_pwrite(1'b0), .nl_i_cfg_paddr(24'b0), .nl_i_cfg_pwdata(32'b0),
    .nl_i_cfg_pstrb(4'b0), .nl_i_cfg_pprot(3'b0),
    .nl_t_ram_psel(ram_sel), .nl_t_ram_penable(ram_enable), .nl_t_ram_pwrite(ram_write),
    .nl_t_ram_paddr(ram_address), .nl_t_ram_pwdata(ram_data), .nl_t_ram_pstrb(ram_strobe),
    .nl_t_ram_pready(1'b1), .nl_t_ram_pslverr(1'b0),
    .nl_t_ram_prdata(32'hca1c0000 ^ {12'b0, ram_address}));

  integer accesses = 0, cycle = 0, checks = 0;
  reg [19:0] seen_address;
  reg seen_write;
  reg [31:0] seen_data;
  reg [3:0] seen_strobe;
  always @(posedge clk) begin
    cycle <= cycle + 1;
    if (ram_sel && ram_enable) begin
      accesses <= accesses + 1;
      seen_address <= ram_address;
      seen_write <= ram_write;
      seen_data <= ram_data;
      seen_strobe <= ram_strobe;
    end
  end

  task transfer(input [19:0] addr, local_addr, input do_write, miss, input string label);
    integer start_access, start_cycle;
    begin
      @(negedge clk);
      start_access = accesses;
      sel = 1;
      address = addr;
      write = do_write;
      data = 32'h57120000 ^ {12'b0, addr};
      @(negedge clk);
      enable = 1;
      @(posedge clk);
      start_cycle = cycle;
      while (ready !== 1'b1 && cycle - start_cycle < 60) @(posedge clk);
      if (ready !== 1'b1 || error !== miss ||
          (!do_write && !miss && answer !== (32'hca1c0000 ^ {12'b0, local_addr})))
        $fatal(1, "CHECK_FAIL %s source addr=%h ready=%b error=%b answer=%h",
               label, addr, ready, error, answer);
      $display("ACCESS %s write=%0d cycles=%0d", label, do_write, cycle - start_cycle + 1);
      @(negedge clk);
      if (accesses - start_access != (miss ? 0 : 1) ||
          (!miss && (seen_address !== local_addr || seen_write !== do_write ||
                     (do_write && (seen_data !== data || seen_strobe !== 4'hf)))))
        $fatal(1, "CHECK_FAIL %s target addr=%h got=%h accesses=%0d",
               label, addr, seen_address, accesses - start_access);
      checks = checks + 1;
      sel = 0;
      enable = 0;
    end
  endtask

  task window_edges(input [19:0] addr, local_addr, input string label);
    begin
      transfer(addr, local_addr, 0, 0, label);
      transfer(addr, local_addr, 1, 0, label);
      transfer(addr + 20'hffc, local_addr + 20'hffc, 0, 0, label);
      transfer(addr + 20'hffc, local_addr + 20'hffc, 1, 0, label);
    end
  endtask

  task grouped_windows;
    window_edges(20'h00000, 20'h00000, "unselected");
    transfer(20'hffffc, 20'hffffc, 0, 0, "unselected_last");
    transfer(20'hffffc, 20'hffffc, 1, 0, "unselected_last");
    modes = ALL;
    window_edges(20'h10000, 20'h18000, "all_selectors");
    window_edges(20'h01000, 20'h02000, "first_group");
    window_edges(LAST_PAGE, LAST_PAGE << 1, "last_group");
    modes = ALL ^ LAST;
    window_edges(LAST_PAGE, 20'h14000, "highest_false");
    transfer(20'h10000, 0, 0, 1, "missing_selector");
    modes = 1;
    transfer(20'h90000, 0, 0, 1, "hole");
    transfer(20'h90000, 0, 1, 1, "hole");
  endtask

  task independent_windows;
    modes = 1;
    window_edges(20'h01000, 20'h02000, "first_independent");
    transfer(20'h02000, 0, 0, 1, "second_disabled");
    modes = LAST | 2;
    window_edges(20'h02000, 20'h04000, "second_independent");
    window_edges(20'h20000, 20'h40000, "last_independent");
    transfer(20'h01000, 0, 1, 1, "first_disabled");
    modes = ALL;
    window_edges(20'h10000, 20'h20000, "middle_independent");
    transfer(20'h90000, 0, 0, 1, "hole");
    transfer(20'h90000, 0, 1, 1, "hole");
  endtask

  initial begin
    repeat (6) @(negedge clk);
    reset_n = 1;
    repeat (6) @(negedge clk);
    if (INDEPENDENT)
      independent_windows();
    else
      grouped_windows();
    $display("ADDRESS_PASS selectors=%0d checks=%0d", COUNT, checks);
    $finish;
  end
endmodule
