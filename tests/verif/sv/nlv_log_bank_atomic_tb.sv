`timescale 1ns/1ps
module tb;
  reg clk = 0, reset_n = 0;
  always #5 clk = ~clk;
  reg psel = 0, penable = 0, pwrite = 0;
  reg [31:0] paddr = 0;
  reg [31:0] pwdata = 0;
  reg [3:0] pstrb = 0;
  wire pready, pslverr, alarm;
  wire [31:0] prdata;
`ifdef CROSSED
  reg clk_slow = 0;
  always #13.5 clk_slow = ~clk_slow;
  reg data_psel = 0, data_penable = 0;
  wire data_pwrite = 0;
  wire [31:0] data_paddr = 32'h234;
  wire data_pready, data_pslverr;
  localparam PHASES = 40;
`else
  reg arvalid = 0;
  wire arready, rvalid, rlast;
  wire [15:0] rid;
  wire [1:0] rresp;
  wire [31:0] araddr = @ADDRESS@;
  wire [15:0] arid = @AXID@;
  wire [2:0] arsize = 2;
  wire aruser = 1;
  wire t_arvalid, t_rready;
  wire [15:0] t_arid;
  reg pending = 0, release_error = 0;
  reg [15:0] t_rid = 0;
  wire t_arready = !pending;
  wire t_rvalid = pending && release_error;
  wire t_rlast = 1;
  wire [1:0] t_rresp = 2;
  localparam PHASES = 10;
`endif
  integer checked = 0, cycle = 0, alarm_at, reply_at;
  integer before_head = 0, same_edge = 0, after_head = 0;
  integer heads, offers;
  time head_at, event_at;
  bit checking = 0;
  nocloom_atomic_log dut (
@PORTS@
  );

  always @(posedge @BANK_CLOCK@) begin
    if (checking) begin
      if (psel && !pwrite && dut.@READ_HEAD@) begin
        head_at = $time;
        heads = heads + 1;
      end
      if (dut.@RECORD_OFFERED@) begin
        event_at = $time;
        offers = offers + 1;
      end
    end
  end

  always @(posedge clk) cycle <= cycle + 1;
`ifndef CROSSED
  always @(posedge clk) begin
    if (!reset_n) begin
      pending <= 0;
      t_rid <= 0;
    end else if (t_arvalid && t_arready) begin
      pending <= 1;
      t_rid <= t_arid;
    end else if (t_rvalid && t_rready) begin
      pending <= 0;
    end
  end
`endif

  task automatic clocks(input integer count);
    repeat (count) @(negedge clk);
  endtask

  task automatic access(input bit write, input [31:0] addr, input [31:0] data,
                        output reg [31:0] value);
    integer elapsed;
    begin
      psel = 1; penable = 0; pwrite = write; paddr = addr;
      pwdata = data; pstrb = write ? 4'hf : 0;
      clocks(1); penable = 1;
      @(posedge clk);
      for (elapsed = 0; !pready && elapsed < 100; elapsed = elapsed+1) @(posedge clk);
      if (pready !== 1 || pslverr !== 0) $fatal(1, "APB response addr=%h", addr);
      value = prdata;
      clocks(1); psel = 0; penable = 0;
    end
  endtask

  task automatic target_request;
`ifndef CROSSED
    begin
      arvalid = 1;
      do @(posedge clk); while (!arready);
      clocks(1); arvalid = 0;
      while (!pending) clocks(1);
    end
`endif
  endtask

  task automatic fail_request;
    begin
`ifdef CROSSED
      data_psel = 1;
      clocks(1); data_penable = 1;
      do @(posedge clk); while (!data_pready);
      if (data_pslverr !== 1) $fatal(1, "watched APB response");
      clocks(1); data_psel = 0; data_penable = 0;
`else
      release_error = 1;
      do @(posedge clk); while (!rvalid);
      if (rid !== arid || rresp !== 2 || rlast !== 1) $fatal(1, "AXI response");
      reply_at = cycle;
      clocks(1); release_error = 0;
`endif
    end
  endtask

  task automatic sweep(input [31:0] addr, input [31:0] captured, input bit overflow = 0);
    integer phase, prior, equal, later;
    reg [31:0] current, expected, settled, unused_read, before_capture;
    begin
      prior = 0; equal = 0; later = 0;
      before_capture = addr == @BANK@ + 4 ? 32'h100 : 0;
      for (phase = -PHASES; phase <= PHASES; phase = phase+1) begin
        reset_n = 0; clocks(5); reset_n = 1; clocks(8);
        access(1, @BANK@ + 4, 32'h100, unused_read);
        if (overflow) begin
          target_request;
          fail_request;
          clocks(PHASES);
          if (alarm !== 1) $fatal(1, "first error did not raise the alarm");
          before_capture = captured ^ 32'h2;
        end
        target_request;
        heads = 0; offers = 0;
        checking = 1;
        fork
          begin
            if (phase < 0) clocks(-phase);
            access(0, addr, 0, current);
          end
          begin
            if (phase > 0) clocks(phase);
            fail_request;
          end
          begin
            if (!overflow) begin
              @(posedge alarm);
              alarm_at = cycle;
            end
          end
        join
`ifndef CROSSED
        if (!overflow && alarm_at != reply_at + 2)
          $fatal(1, "alarm phase=%0d alarm=%0d reply=%0d", phase, alarm_at, reply_at);
`endif
        if ((phase == -PHASES && current !== captured) ||
            (phase == PHASES && current !== before_capture))
          $fatal(1, "capture outside access addr=%h phase=%0d value=%h", addr, phase, current);
`ifdef CROSSED
        if (overflow) clocks(PHASES);
`endif
        checking = 0;
        if (heads != 1 || offers != 1)
          $fatal(1, "read observation addr=%h phase=%0d heads=%0d offers=%0d",
                 addr, phase, heads, offers);
        if (event_at < head_at) prior = prior + 1;
        else if (event_at == head_at) equal = equal + 1;
        else later = later + 1;
        // State updates on the head edge become visible after the sampled word.
        expected = event_at < head_at ? captured : before_capture;
        if (current !== expected)
          $fatal(1, "EDGE_WORD addr=%h phase=%0d head=%0t event=%0t actual=%h expected=%h",
                 addr, phase, head_at, event_at, current, expected);
        access(0, addr, 0, settled);
        if (settled !== captured) $fatal(1, "captured word addr=%h actual=%h expected=%h",
                                        addr, settled, captured);
        checked = checked + 1;
      end
      if (prior == 0 || equal == 0 || later == 0)
        $fatal(1, "capture sweep addr=%h prior=%0d equal=%0d later=%0d", addr, prior, equal, later);
      before_head = before_head + prior;
      same_edge = same_edge + equal;
      after_head = after_head + later;
    end
  endtask

  initial begin
@CHECKS@
    $display("CHECK_PASS checks=%0d prior=%0d equal=%0d later=%0d",
             checked, before_head, same_edge, after_head);
    $finish;
  end
  initial begin
    #5000000;
    $fatal(1, "watchdog");
  end
endmodule
