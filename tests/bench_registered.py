"""Registered outputs for testbench target models.

A target model updates its state with blocking assignments at the clock's
rising edge. If its outputs were continuous assignments of that state, a
simulator that runs the model before the design under test would let the
design sample the values after the edge (a race between processes). This
helper turns every `assign <port> = <expression>;` of a model into a register
loaded at the falling edge: the design samples, at each rising edge, only
values computed half a cycle earlier from the state before that edge."""
import re

_ASSIGN = re.compile(r'^assign (nl_t_\S+?) = (.*?);\s*$', re.M | re.S)


def registered(text, clock):
    """Return (model text, driven port names). The ports become regs that the
    caller declares; they are zero before the first falling edge."""
    ports, loads = [], []
    for match in _ASSIGN.finditer(text):
        ports.append(match.group(1))
        loads.append(f' {match.group(1)} <= {match.group(2)};')
    body = _ASSIGN.sub('', text)
    block = ('initial begin ' + ' '.join(f'{p} = 0;' for p in ports) + ' end\n' +
             f'always @(negedge {clock}) begin\n' + '\n'.join(loads) + '\nend\n')
    return body + block, ports
