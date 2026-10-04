"""Explicit scale graphs, independent of the C++ tree helper."""


def domain(shape, kind, index):
    """The clock domain of source or target index (c64: each endpoint its own)."""
    if shape != "c64" or (kind == "s" and index == 0):
        return "main"
    return f"d{kind}{index:02}"


def configuration(protocol, shape):
    clocked = shape == "c64"
    inputs = 32 if clocked else 64 if shape.startswith("64") else int(shape)
    # 256: 256 sources and 256 targets on a balanced 64-node tree.
    nodes = 64 if inputs == 256 else 32 if inputs == 64 else 16
    targets = (1 if shape == "64h1" else inputs if inputs in (64, 256) else 32 if clocked
               else 65 - inputs)
    chain = shape == "64h1"
    names = [f"node{i:02}" for i in range(nodes)]
    edges = [
        (names[i - 1 if chain else (i - 1) // 2], names[i]) for i in range(1, nodes)
    ]
    sources = [dict(endpoint=f"src{i:02}", node=names[0]) for i in range(inputs)]
    owners = [
        dict(
            endpoint=f"bank{i:02}",
            node=names[-1] if chain else names[1 + i % (nodes - 1)],
        )
        for i in range(targets)
    ]
    graph = {}
    for direction in ("request", "response"):
        # c64: request node i in the domain of source 2i, response node i in that of target 2i.
        kind = "s" if direction == "request" else "t"
        graph[direction] = dict(
            nodes=[dict(name=n, **(dict(clock_domain=domain(shape, kind, 2 * i)) if clocked else {}))
                   for i, n in enumerate(names)],
            links=[
                {
                    "from": a if direction == "request" else b,
                    "to": b if direction == "request" else a,
                }
                for a, b in edges
            ],
            entries=sources if direction == "request" else owners,
            exits=owners if direction == "request" else sources,
        )
    return dict(
        schema_version=10,
        name=f"scale_{protocol}_n{nodes}_s{inputs}_"
        + ("chain" if chain else "clocked" if clocked else "balanced"),
        address_width=32,
        reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")],
        clock_domains=[
            dict(name=n, clock_port="clk_" + n, reset_port="reset_n", test_port="test")
            for n in dict.fromkeys(
                [domain(shape, "s", i) for i in range(inputs)]
                + [domain(shape, "t", i) for i in range(targets)])
        ],
        initiators=[
            dict(
                name=f"src{i:02}",
                clock_domain=domain(shape, "s", i),
                protocol="apb4" if protocol == "apb" else "axi4_lite",
                data_width=32,
                # 256 axi: the first source keeps two transfers in flight, so it records one of
                # the 256 routing windows as each transfer's destination.
                **(dict(max_outstanding=2) if shape == "256" and protocol == "axi" and i == 0
                   else {}),
            )
            for i in range(inputs)
        ],
        targets=[
            dict(
                name=f"bank{i:02}",
                clock_domain=domain(shape, "t", i),
                protocol="apb4",
                attributes="amba",
                data_width=32,
                mappings=[dict(base=str(i * 4096), size="4096")],
            )
            for i in range(targets)
        ],
        arbitration=dict(policy="round_robin"),
        topology=graph,
        connectivity="all",
        output=dict(backend="verilog", language="verilog_2001"),
    )
