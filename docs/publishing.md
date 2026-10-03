# Publishing pyXcom

Releases use GitHub Actions and PyPI trusted publishing (OIDC). No PyPI API token,
GitHub secret, or local `.pypirc` is needed. TestPyPI and PyPI use separate
publisher configurations. The existing production
release uses the `pypi` environment. Use the optional TestPyPI staging path when
its account, project publisher, and `testpypi` GitHub environment are configured.

## Configure the trusted publishers

On an index you intend to use, sign in and open account settings → Publishing.
For a new project,
add a pending GitHub publisher using these exact values. If `pyXcom` already exists
under your account, add a publisher from that project's Publishing settings instead.

| Setting | TestPyPI | PyPI |
| --- | --- | --- |
| Project name | `pyXcom` | `pyXcom` |
| Owner | `haochengw372-hash` | `haochengw372-hash` |
| Repository name | `pyXcom` | `pyXcom` |
| Workflow filename | `publish.yml` | `publish.yml` |
| Environment name | `testpypi` | `pypi` |

Configuration pages: [TestPyPI](https://test.pypi.org/manage/account/publishing/)
and [PyPI](https://pypi.org/manage/account/publishing/).

In the GitHub repository's Settings → Environments, retain `pypi` for production
and create `testpypi` only when enabling staging. Names must match the relevant
publisher configurations. Configure deployment branch/tag
restrictions for the release refs you use; a required reviewer can be added for
production releases.

## Optional TestPyPI staging and installation verification

1. Review the version in `pyproject.toml`, metadata, license, README, and the files
   included in the wheel and source distribution. Commit and push the release.
2. In GitHub Actions, select **Publish Python package** → **Run workflow**. Select
   the release ref and `repository: testpypi`.
3. Wait for the build and both wheel test jobs to succeed. The build job runs
   `twine check` and inspects the wheel and source archive for research data,
   cookies, environment files, caches, and unreleased scheduler modules. Wheel tests
   run on Python 3.10 and 3.13 using standard-library `unittest`; no pytest
   dependency is installed. Each job installs the built wheel in a clean virtual
   environment, copies tests to a temporary directory outside the checkout,
   verifies that imports come from that environment, and checks dependencies
   and the CLI. Only then does the publish job upload those exact build artifacts
   using its OIDC identity.
4. Install the TestPyPI release in a new environment. For version `1.0.1`:

   ```bash
   python3 -m venv /tmp/pyxcom-testpypi-101
   /tmp/pyxcom-testpypi-101/bin/python -m pip install \
     'browser-cookie3>=0.19,<1' 'beautifulsoup4>=4.12,<5' 'httpx>=0.27,<1'
   /tmp/pyxcom-testpypi-101/bin/python -m pip install \
     --index-url https://test.pypi.org/simple/ --no-deps 'pyXcom==1.0.1'
   /tmp/pyxcom-testpypi-101/bin/python -m pip check
   /tmp/pyxcom-testpypi-101/bin/python -c \
     'from importlib.metadata import version; from pyxcom import XClient; print(version("pyXcom"))'
   /tmp/pyxcom-testpypi-101/bin/pyxcom --help
   ```

   Dependencies come from the normal PyPI index, while the package under test
   comes only from TestPyPI. This avoids resolving dependencies from a mixed index.
   Run the same copied test suite from a directory outside the source checkout
   when checking a release manually. Setting `PYTHONPATH=src` would test the
   working tree instead of the downloaded release. Update the version and
   temporary directory for subsequent releases.

## Publish to PyPI

When TestPyPI staging is configured, verify its installation first and dispatch
the same workflow at the same commit with `repository: pypi`. For an existing
production publisher without staging, verify the wheel and source distribution
locally in clean environments, then dispatch `repository: pypi`; the workflow
also requires its archive checks and both installed-wheel test jobs to pass.
A release tag makes selecting the same source
revision straightforward. This dispatch rebuilds and checks distributions from
that revision; it does not transfer the earlier TestPyPI artifact.

After the production jobs succeed, verify the public index in another new virtual
environment:

```bash
python3 -m venv /tmp/pyxcom-pypi-101
/tmp/pyxcom-pypi-101/bin/python -m pip install --index-url https://pypi.org/simple/ 'pyXcom==1.0.1'
/tmp/pyxcom-pypi-101/bin/python -m pip check
/tmp/pyxcom-pypi-101/bin/python -c 'from importlib.metadata import version; from pyxcom import XClient; print(version("pyXcom"))'
/tmp/pyxcom-pypi-101/bin/pyxcom --help
```

The workflow is manually dispatched; pull requests and branch pushes do not
publish packages. Only the publish job has `id-token: write`. Publishing the same
filename again fails instead of silently skipping it. Correct an unpublished
release locally, or increment the version for changes after publication.

References: [PyPI trusted publishers](https://docs.pypi.org/trusted-publishers/)
and [PyPA publishing action](https://github.com/pypa/gh-action-pypi-publish).

## 1.0 acceptance gates

In addition to installed-wheel tests, exercise recovery against isolated copies: a stale derived table must prepare and verify without changing source bytes; changed source hashes, unavailable latest checkpoint evidence, invalid generations, and query/binding changes against the frozen plan or previous receipt must remain blocked. Verify that an explicit apply preserves complete/partial state and that an old table replacement is detected by the independent receipt. Do not run release recovery tests against live research directories.

The distribution includes the integrity/recovery library, documentation, and standard-library regression tests. Incomplete multi-account scheduler drafts remain excluded. Publishing does not upgrade or restart an existing research process.
