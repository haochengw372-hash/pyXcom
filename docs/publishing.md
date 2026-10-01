# Publishing pyXcom

Releases use GitHub Actions and PyPI trusted publishing (OIDC). No PyPI API token,
GitHub secret, or local `.pypirc` is needed. TestPyPI and PyPI have separate accounts
and publisher configurations. Set up both before publishing.

## Configure the trusted publishers

On each index, sign in and open account settings → Publishing. For a new project,
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

In the GitHub repository's Settings → Environments, create `testpypi` and `pypi`.
The names must match the publisher configurations. Configure deployment branch/tag
restrictions for the release refs you use; a required reviewer can be added for
production releases.

## Publish to TestPyPI and verify installation

1. Review the version in `pyproject.toml`, metadata, license, README, and the files
   included in the wheel and source distribution. Commit and push the release.
2. In GitHub Actions, select **Publish Python package** → **Run workflow**. Select
   the release ref and `repository: testpypi`.
3. Wait for both jobs to succeed. The build job builds the distributions, runs
   `twine check`, installs the wheel into a clean virtual environment, runs the
   unit tests, and checks the CLI. The separate publish job downloads those exact
   artifacts and uploads them with an OIDC identity.
4. Install the TestPyPI release in a new environment. For version `0.7.0`:

   ```bash
   python3 -m venv /tmp/pyxcom-testpypi-070
   /tmp/pyxcom-testpypi-070/bin/python -m pip install \
     'browser-cookie3>=0.19,<1' 'beautifulsoup4>=4.12,<5' 'httpx>=0.27,<1'
   /tmp/pyxcom-testpypi-070/bin/python -m pip install \
     --index-url https://test.pypi.org/simple/ --no-deps 'pyXcom==0.7.0'
   /tmp/pyxcom-testpypi-070/bin/python -m pip check
   /tmp/pyxcom-testpypi-070/bin/python -c \
     'from importlib.metadata import version; from pyxcom import XClient; print(version("pyXcom"))'
   /tmp/pyxcom-testpypi-070/bin/pyxcom --help
   ```

   Dependencies come from the normal PyPI index, while the package under test
   comes only from TestPyPI. This avoids resolving dependencies from a mixed index.
   Update the version and temporary directory for subsequent releases.

## Publish to PyPI

After the TestPyPI installation succeeds, dispatch the same workflow at the same
commit with `repository: pypi`. A release tag makes selecting the same source
revision straightforward. This dispatch rebuilds and checks distributions from
that revision; it does not transfer the earlier TestPyPI artifact.

After the production jobs succeed, verify the public index in another new virtual
environment:

```bash
python3 -m venv /tmp/pyxcom-pypi-070
/tmp/pyxcom-pypi-070/bin/python -m pip install --index-url https://pypi.org/simple/ 'pyXcom==0.7.0'
/tmp/pyxcom-pypi-070/bin/python -m pip check
/tmp/pyxcom-pypi-070/bin/python -c 'from importlib.metadata import version; from pyxcom import XClient; print(version("pyXcom"))'
/tmp/pyxcom-pypi-070/bin/pyxcom --help
```

The workflow is manually dispatched; pull requests and branch pushes do not
publish packages. Only the publish job has `id-token: write`. Publishing the same
filename again fails instead of silently skipping it. Correct an unpublished
release locally, or increment the version for changes after publication.

References: [PyPI trusted publishers](https://docs.pypi.org/trusted-publishers/)
and [PyPA publishing action](https://github.com/pypa/gh-action-pypi-publish).
