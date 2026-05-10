# Local Experiment Workflow

This file documents the local scripts added for mutation-based UT generation
experiments on Multi-SWE-bench instances.

## Entry Point

All local helper scripts are exposed through:

```bash
python run.py <command> [options]
```

Currently supported command:

```bash
python run.py checkout-bug
```

## Checkout Bug Versions

The `checkout-bug` command reads Multi-SWE-bench JSONL files and checks out each
selected instance to its bug version, using the instance's `base.sha`.

Default input:

```text
data/datasets/Multi-SWE-bench/java/*.jsonl
```

Default output:

```text
data/checkout_bug/<instance_id>/
```

Example:

```bash
python run.py checkout-bug \
  --dataset-files data/datasets/Multi-SWE-bench/java/google__gson_dataset.jsonl \
  --specifics google__gson-1093
```

This creates:

```text
data/checkout_bug/google__gson-1093/
```

The checkout is placed on a local branch:

```text
bug/google__gson-1093
```

and `HEAD` is reset to the JSONL instance's `base.sha`.

## Useful Options

Checkout all default Java instances:

```bash
python run.py checkout-bug
```

Checkout one specific instance:

```bash
python run.py checkout-bug --specifics google__gson-1093
```

You can also use PR-style identifiers:

```bash
python run.py checkout-bug --specifics google/gson:pr-1093
```

Recreate an existing checkout:

```bash
python run.py checkout-bug --specifics google__gson-1093 --force
```

Use custom dataset files:

```bash
python run.py checkout-bug \
  --dataset-files data/datasets/Multi-SWE-bench/java/google__gson_dataset.jsonl
```

Use custom repository cache and output directories:

```bash
python run.py checkout-bug \
  --repo-dir data/repos \
  --output-dir data/checkout_bug
```

