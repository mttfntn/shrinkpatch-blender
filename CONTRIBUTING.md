# Contributing to Shrink Patch

Thanks for helping! Bug reports, ideas and pull requests are all welcome.

## Reporting a bug or suggesting a feature

Open an [issue](https://github.com/mttfntn/shrinkpatch-blender/issues/new/choose) and pick a form:

- **Bug report:** say which Blender version and add-on version you use, and the steps to reproduce. A small `.blend` file helps most.
- **Feature request:** describe what you're trying to do, not only the solution you have in mind.

First, check whether someone has already opened an issue for the same thing. Also look at the README's [Troubleshooting](README.md#troubleshooting) section: some effects there are expected.

## Changing the code

[HANDOVER.md](HANDOVER.md) explains how the add-on is organised and the pitfalls already found. Read it before a larger change.

1. **Fork** the repository and create a branch for your change.
2. You need **Blender 5.3** (the only supported version) and Python 3.9 or newer.
3. Run the tests. They start their own background Blender with a throw-away profile, so your Blender setup is never touched:

   ```
   python tests/run.py
   ```

   Expected result: `SHRINKPATCH_TESTS ... failures=0 errors=0`. If Blender isn't found, add `--blender "path/to/blender"`.
4. **If you fix a bug,** add a test to `tests/test_shrinkpatch.py` that fails without your fix.
5. **If your change could alter results,** compare behaviour with `main` using the snapshot tools (HANDOVER.md, "How to make a change") and explain any difference in the pull request.
6. **Open a pull request.** The template has a short checklist. CI runs the full test suite on Blender 5.3.

### Style

- The add-on is one module, `shrinkpatch/__init__.py`, divided into sections. Keep new code in the matching section.
- Give new functions a docstring that says what they do.
- **Panels:** use `#` comments, not docstrings, because Blender shows a panel's docstring as its tooltip.
- **Operator docstrings** are user tooltips: write them for users.
- Keep commits small, with messages that explain what changed and how you checked it.

## License

Shrink Patch is GPL-3.0-or-later. By contributing, you agree that your contribution is released under the same license.
