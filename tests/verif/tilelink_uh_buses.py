"""Independent byte memories for TileLink UH to peripheral bus transactions."""


def replace_model(body, protocol):
    start = body.index("  always @(posedge target_clk)")
    end = body.index("  task expect_message", start)
    if protocol == "apb4":
        model = r"""
  always @* begin
    t_pready=(cycle%5!=0);
    t_pslverr=t_pwrite ? aw_count==fail_b : (t_paddr & ~3)==fail_r;
    t_prdata=0;
    for(integer b=0;b<4;b=b+1) t_prdata[b*8+:8]=memory[(t_paddr & ~3)+b];
  end
  always @(posedge target_clk) if(reset_n && t_psel && t_penable && t_pready) begin
    if(t_pwrite) begin
      aw_count=aw_count+1; w_count=w_count+1; b_count=b_count+1;
      for(integer b=0;b<4;b=b+1) if(t_pstrb[b]) memory[(t_paddr & ~3)+b]=t_pwdata[b*8+:8];
    end else begin
      ar_count=ar_count+1; r_count=r_count+1;
      for(integer b=0;b<4;b=b+1) actual_reads[(t_paddr & ~3)+b]=actual_reads[(t_paddr & ~3)+b]+1;
    end
  end
"""
        # APB reads cover a complete word. Narrow reads are checked separately
        # by their returned active lanes, while the peer records the physical word.
        body = body.replace(
            "for(integer b=0;b<(1<<sz);b=b+1)\n        if(!MISS_LAST || address+b<7168) expected_reads[address+b]=expected_reads[address+b]+1;",
            "for(integer b=0;b<(sz<2 ? 4:1<<sz);b=b+1)\n        expected_reads[(address & ~3)+b]=expected_reads[(address & ~3)+b]+1;",
        )
    elif protocol == "wishbone":
        model = r"""
  always @* begin
    t_ack=t_cyc && t_stb && cycle%5!=0 && !(t_we ? aw_count==fail_b : (t_adr & ~3)==fail_r);
    t_err=t_cyc && t_stb && cycle%5!=0 && (t_we ? aw_count==fail_b : (t_adr & ~3)==fail_r);
    t_dat_r=0;
    for(integer b=0;b<4;b=b+1) t_dat_r[b*8+:8]=memory[(t_adr & ~3)+b];
  end
  always @(posedge target_clk) if(reset_n && t_cyc && t_stb && (t_ack || t_err)) begin
    if(t_adr%4 || t_sel==0) $fatal(1,"ASSERT_FAIL:UH.WB.ADDRESS_SEL");
    if(t_we) begin
      aw_count=aw_count+1; w_count=w_count+1; b_count=b_count+1;
      for(integer b=0;b<4;b=b+1) if(t_sel[b]) memory[(t_adr & ~3)+b]=t_dat_w[b*8+:8];
    end else begin
      ar_count=ar_count+1; r_count=r_count+1;
      for(integer b=0;b<4;b=b+1) if(t_sel[b]) actual_reads[(t_adr & ~3)+b]=actual_reads[(t_adr & ~3)+b]+1;
    end
  end
"""
    else:
        model = r"""
  reg ahb_pending=0, ahb_write=0, error_phase=0;
  integer ahb_address=0, ahb_size=0;
  wire ahb_error=ahb_pending && (ahb_write ? aw_count==fail_b : (ahb_address & ~3)==fail_r);
  always @* begin
    t_hreadyout=ahb_error ? error_phase : cycle%5!=0;
    t_hresp=ahb_error;
    t_hrdata=0;
    for(integer b=0;b<4;b=b+1) t_hrdata[b*8+:8]=memory[(ahb_address & ~3)+b];
  end
  always @(posedge target_clk) begin
    if(!reset_n) begin ahb_pending=0; error_phase=0; end
    else begin
      if(ahb_error && !error_phase) error_phase=1;
      else if(t_hready) begin
        error_phase=0;
        if(ahb_pending) begin
          if(ahb_write) begin
            aw_count=aw_count+1; w_count=w_count+1; b_count=b_count+1;
            for(integer b=0;b<(1<<ahb_size);b=b+1)
              memory[ahb_address+b]=t_hwdata[(ahb_address%4+b)*8+:8];
          end else begin
            ar_count=ar_count+1; r_count=r_count+1;
            for(integer b=0;b<(1<<ahb_size);b=b+1) actual_reads[ahb_address+b]=actual_reads[ahb_address+b]+1;
          end
        end
        ahb_pending=t_hsel && t_htrans[1];
        if(ahb_pending) begin
          ahb_address=t_haddr; ahb_size=t_hsize; ahb_write=t_hwrite;
          if(t_hsize>2 || t_haddr%(1<<t_hsize)) $fatal(1,"ASSERT_FAIL:UH.AHB.ADDRESS_SIZE");
        end
      end
    end
  end
"""
    # Recompute the insertion offsets after changing the read footprint above.
    start = body.index("  always @(posedge target_clk)")
    end = body.index("  task expect_message", start)
    return body[:start] + model + body[end:]
