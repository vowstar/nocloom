    begin : peripheral_errors
      fail_b=aw_count; put(0,4,1,4096,-1);
      fail_b=-1; put(0,4,1,4096,-1); get(4,2,4096);
      fail_r=4100; get(4,2,4096);
      fail_r=-1; get(4,2,4096);
      $display("UH_PERIPHERAL_ERRORS write=1 read=1 recovery=1");
    end
