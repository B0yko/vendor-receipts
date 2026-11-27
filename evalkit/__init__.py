"""Evaluation of Vendor Receipts (lane L4). The only package that reads the labelled CSV in data/.

labels: rows with their dev/holdout split; leak: the dataset-leak scanner behind tests/test_leak.py;
report: colors, baselines, regressions, flags and errors per split; baseline: the plain one-call agent.
Nothing here prints label rows: reports carry ids, colors and aggregates.
"""
