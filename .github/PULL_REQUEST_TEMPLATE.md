## What

<!-- One paragraph: the behaviour change and why. Link the issue if there is one. -->

## How it was tested

- [ ] `make check` passes (ruff, mypy, schema check, pytest)
- [ ] New or changed behaviour is covered by a test against `tests/fake_controller.py`
- [ ] Tested against a real controller (firmware version, what the controller did, log evidence):

## Checklist

- [ ] CHANGELOG entry under *Unreleased*
- [ ] New flags default to off and leave `--preset live` unchanged
- [ ] No personal identifiers, captures or certificates in the diff
- [ ] No firmware images, decompiled code or other Google-authored material (see `NOTICE`)
