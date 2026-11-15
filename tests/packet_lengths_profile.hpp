#pragma once
#include "core/axi4_packetizer.hpp"
#include "core/axi4_upsize.hpp"
#include "core/axi4_width_clamp.hpp"
#include "core/burst_faces.hpp"
#include "core/decode_packet.hpp"
#include <string>

inline nocloom::Result<nocloom::detail::NodeHardware>
packet_length_hardware(const std::string &kind, unsigned bits) {
  using namespace nocloom::detail;
  if (kind == "markers") {
    logic::Logic l;
    const auto length = l.in("test", "length", bits);
    const auto data = l.in("test", "data", 128);
    const auto address = l.in("test", "address", 32);
    l.out("test", "encoded", decode_packet::word_data(l, length, 128));
    l.out("test", "decoded", decode_packet::length_of(l, data, bits));
    l.out("test", "wrap_end", wrap_end_of(l, l.bit(1), l.num(2, 2), length, l.num(3, 2), address));
    return l.finish("nocloom_packet_length_markers");
  }
  if (kind == "packetizer") {
    Axi4PacketizerProfile p;
    p.name = "lengths";
    p.packet_len_bits = bits;
    Axi4PacketWindow window;
    window.window = {0, 65536};
    window.max_bytes = window.boundary_bytes = 4096;
    window.align_bytes = 4;
    p.windows.push_back(window);
    return lower_axi4_packetizer(p);
  }
  if (kind == "upsize") {
    Axi4UpsizeProfile p;
    p.name = "lengths";
    p.packet_len_bits = bits;
    p.data_bits = 128;
    return lower_axi4_upsize(p);
  }
  if (kind == "clamp") {
    Axi4WidthClampProfile p;
    p.name = "lengths";
    p.packet_len_bits = bits;
    p.data_bits = 128;
    p.windows = {{{0, 65536}, 2}};
    return lower_axi4_width_clamp(p);
  }
  Axi4WidthCutProfile p;
  p.name = "lengths";
  p.packet_len_bits = bits;
  p.data_bits = 128;
  p.down_bits = 32;
  p.fixed_width = 2;
  if (kind == "cut_axi")
    p.piece_len_bits = {{{0, 65536}, 8}};
  return lower_axi4_width_cut(p);
}
