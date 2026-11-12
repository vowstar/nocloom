"""Mesh helper structure, reproducible examples and AXI4 transactions."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def run(argv, log, timeout=180):
    result = subprocess.run([str(a) for a in argv], capture_output=True,
                            text=True, timeout=timeout)
    log.write_text(result.stdout + result.stderr)
    assert result.returncode == 0, (argv, result.stdout[-4000:], result.stderr[-4000:])


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--unit')
    p.add_argument('--example')
    p.add_argument('--cli')
    p.add_argument('--case')
    p.add_argument('--transactions', action='store_true')
    p.add_argument('--work', required=True)
    args = p.parse_args()
    base = Path(args.work).resolve()
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='mesh-', dir=base))
    (base / 'latest.txt').write_text(str(work) + '\n')
    if args.unit:
        run([args.unit], work / 'unit.log')
        return
    generated = work / 'helper'
    run([args.example, generated], work / 'example.log')
    if args.transactions:
        driver = Path(__file__).parent / 'verif/nlverif/run.py'
        case = Path(args.case)
        run([sys.executable, driver, '--case', case, '--work', work / 'transactions',
             '--param', f'generated={generated}', '--negative'],
            work / 'transactions.log', timeout=500)
        return
    repeated = work / 'repeated'
    run([args.example, repeated], work / 'repeated.log')
    expected = {f.name: f.read_bytes() for f in generated.iterdir()}
    assert expected == {f.name: f.read_bytes() for f in repeated.iterdir()}, 'REPEATED_BYTES'
    shipped = Path(__file__).resolve().parents[1] / 'examples/mesh_axi.yaml'
    assert shipped.read_bytes() == (generated / 'mesh_axi.yaml').read_bytes(), 'YAML_BYTES'
    for label, yaml in [('generated', generated / 'mesh_axi.yaml'), ('shipped', shipped)]:
        directory = work / label
        run([args.cli, 'generate', yaml, '--out-dir', directory], work / f'{label}.log')
        actual = {f.name: f.read_bytes() for f in directory.iterdir()}
        assert actual == {k: v for k, v in expected.items() if k != 'mesh_axi.yaml'}, 'CLI_BYTES'
    mapping = json.loads((generated / 'nocloom_mesh_axi.map.json').read_text())
    transactions = mapping['definition']['endpoint_transactions']
    assert transactions['initiators'] == {
        f'i{r}{c}': dict(id_width=2, len_width=8) for r in (0, 2) for c in (0, 2)}
    assert transactions['targets'] == {
        f't{r}{c}': dict(id_width=4, len_width=8) for r in range(3) for c in range(3)}



if __name__ == '__main__':
    main()
