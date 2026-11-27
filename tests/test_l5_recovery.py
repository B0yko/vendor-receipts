"""Offline blind-run recovery tests. A subprocess proves shutdown does not join a stuck worker."""
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from app import ledger, pipeline, run
from app.out import trace


class Recovery(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.input = self.root / 'cases.csv'
        self.input.write_text('id,url,product,vendor\na,https://example.org,Acme,Acme Ltd\nb,https://example.com,B,B Ltd\n')
        self.out = self.root / 'v.jsonl'
        self.submit = self.root / 'submission'

    def rows(self, path):
        with path.open(newline='') as f:
            return list(csv.DictReader(f))

    def test_rebuild_missing_duplicate_and_torn_line(self):
        cases = run.read_cases(self.input)
        base = {'id': 'b', 'url': cases[1]['url'], 'overall': 'Green'}
        saved = [base, base | {'overall': 'Red'}, base | {'overall': 'Yellow', 'error': 'failed retry'}]
        self.out.write_text(''.join(json.dumps(v) + '\n' for v in saved) + '{"id":')
        before = self.out.read_bytes()
        with mock.patch.object(run, 'run_cases', side_effect=AssertionError('unexpected research')):
            self.assertEqual(run.main(['--cases', str(self.input), '--from-jsonl', str(self.out),
                                       '--submit', str(self.submit)]), 0)
        self.assertEqual(before, self.out.read_bytes())
        for path in (self.out.with_suffix('.csv'), self.out.with_suffix('.full.csv'), self.submit/'stackgini-verdicts.csv'):
            a, b = self.rows(path)
            self.assertEqual((a['url'], a['overall'], b['url'], b['overall']),
                             (cases[0]['url'], 'Yellow', cases[1]['url'], 'Red'))
            self.assertEqual(a['reasoning'], run.NO_VERDICT)
            self.assertFalse(any(a[k] for k in ('red_flags', 'yellow_flags', 'green_flags')))
        self.assertIn(run.NO_VERDICT, (self.submit/'traces/a.md').read_text())

    def test_multiwave_deadlines(self):
        seen = []
        clock = [1000.0]

        def check(url, run_dir, deadline, **kw):
            seen.append((deadline, os.getenv('MODEL_HARD_STOP')))
            clock[0] += 10
            return {'url': url, 'overall': 'Green'}

        with mock.patch.object(run.time, 'time', side_effect=lambda: clock[0]), mock.patch.object(
                pipeline, 'check_vendor', side_effect=check), mock.patch.dict(os.environ, MODEL_HARD_STOP='123'):
            with mock.patch('sys.stderr') as stderr:
                run.run_cases(run.read_cases(self.input), str(self.out), 1, 30, runs_dir=str(self.root))
            self.assertIn('warning', str(stderr.write.call_args_list))
            self.assertEqual(os.environ['MODEL_HARD_STOP'], '123')
        self.assertEqual(seen, [(1030, None), (1040, None)])

    def test_stuck_first_wave_does_not_spend_the_next_products_grace(self):
        cases = run.read_cases(self.input)
        cases.append(dict(cases[0], id='c', slug='c'))
        clock, waits = [1000.0], []
        futures = [run.cf.Future() for _ in cases]
        a, b, c = futures
        b.set_result({'id': 'b', 'overall': 'Green'})

        def wait(pending, timeout, return_when):
            waits.append(timeout)
            if len(waits) == 1:
                clock[0] += 10  # b finishes; c starts in its slot
                return {b}, {a}
            clock[0] += timeout
            if len(waits) == 2:
                return set(), {a, c}  # a has expired, c still has ten seconds left
            c.set_result({'id': 'c', 'overall': 'Green'})
            return {c}, set()

        with mock.patch.object(run.cf, 'ThreadPoolExecutor') as pool, mock.patch.object(run.cf, 'wait', wait), \
                mock.patch.object(run.time, 'time', side_effect=lambda: clock[0]), mock.patch.dict(os.environ), \
                mock.patch.object(run.os, '_exit') as exit_process:
            pool.return_value.submit.side_effect = futures
            vs = run.run_cases(cases, str(self.out), 2, 10, runs_dir=str(self.root))
        self.assertEqual(waits, [130, 120, 10])
        self.assertEqual([v.get('error') for v in vs], [run.WALL_ERROR, None, None])
        exit_process.assert_called_once_with(0)

    def test_pipeline_stage_deadlines(self):
        deadline = 12345.0
        def research(url, deadline=None):
            self.assertEqual(deadline, 12345.0)
            return ledger.new_research(url)
        def judge(r, rc, views=None, deadline=None):
            self.assertEqual(deadline, 12345.0)
            self.assertEqual(views, 1)
            return {'overall': 'Green'}
        with mock.patch('app.judge.judge.judge', autospec=judge, side_effect=judge) as j, mock.patch.object(
                pipeline, '_check', return_value=[]), mock.patch.object(pipeline, '_disconfirm', side_effect=lambda r, rc,
                deadline: (r, rc, {})) as d, mock.patch.dict(os.environ, DISCONFIRM='1', AUDIT='0', JUDGE_VIEWS='3'):
            v = pipeline.check_vendor('https://example.org', self.root, deadline, research_fn=research)
            self.assertIsNone(v['error'])
            self.assertEqual(d.call_args.kwargs['deadline'], deadline)
            j.assert_called_once()

    def stuck(self, interrupt):
        # One real worker finishes, one never returns. Advance the clock (or send a real SIGINT)
        # once the coordinator has persisted the first verdict and asks to wait for the second.
        script = '''
import os, signal, sys, threading
from app import run
root, interrupt = sys.argv[1:]
clock = [1000.0]
run.time.time = lambda: clock[0]
entered = threading.Event()
def check(url, run_dir, deadline, **kw):
    assert deadline == 1010 and os.environ['MODEL_HARD_STOP'] == '1070.0'
    if url.endswith('.org'):
        entered.wait(2)
        return {'url': url, 'overall': 'Green'}
    entered.set()
    threading.Event().wait()
run.pipeline.check_vendor = check
wait = run.cf.wait
def bounded(fs, timeout, return_when):
    assert timeout == 130, timeout
    if len(fs) == 1:
        if interrupt == 'yes': os.kill(os.getpid(), signal.SIGINT)
        clock[0] += timeout
        return set(), set(fs)
    return wait(fs, timeout=2, return_when=return_when)
run.cf.wait = bounded
run.main(['--cases',root+'/cases.csv','--out',root+'/v.jsonl','--submit',root+'/submission',
          '--runs-dir',root,'--wall-cap','10','--concurrency','2'])
'''
        p = subprocess.run([sys.executable, '-c', script, str(self.root), 'yes' if interrupt else 'no'],
                           capture_output=True, text=True, timeout=6, env=dict(os.environ, RUN_ID='t'))
        self.assertEqual(p.returncode, 130 if interrupt else 0, p.stderr)
        a, b = [json.loads(x) for x in self.out.read_text().splitlines()]
        self.assertEqual((a['id'], a['overall'], b['id'], b['overall']), ('a', 'Green', 'b', 'Yellow'))
        self.assertEqual(b['error'], 'interrupted by user' if interrupt else run.WALL_ERROR)
        m = json.loads((self.root/'t/manifest.json').read_text())
        self.assertEqual(m['cases'][1]['error'], b['error'])
        self.assertTrue(self.out.with_suffix('.full.csv').exists())
        self.assertEqual(len(self.rows(self.submit/'stackgini-verdicts.csv')), 2)
        self.assertIn(b['error'], (self.submit/'traces/b.md').read_text())

    def test_wall_timeout(self):
        self.stuck(False)

    def test_ctrl_c(self):
        self.stuck(True)

    def test_flag_quote_matches_first_kept_source(self):
        bad = {'id': 'e1', 'kept': False, 'url': 'https://example.org', 'quote': 'Rejected'}
        good = {'id': 'e2', 'kept': True, 'url': 'https://example.com', 'quote': 'Verified'}
        v = {'green_flags': [{'text': 'Acme', 'receipts': ['unknown', 'e1', 'e2'], 'url': bad['url'],
                              'quote': bad['quote']}]}
        md = trace.render({}, [bad, good], v)
        self.assertIn('  - URL: https://example.com\n  - Quote: "Verified"', md)
