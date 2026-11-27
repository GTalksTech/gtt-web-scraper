# The repo is agent-owned and repairs itself without human review

The owner's maintenance budget is one to two hours a month, so detection, diagnosis, fixing, merging and deploying are all automated: the running service files an Incident as a GitHub issue with evidence, an agent in GitHub Actions fixes it and opens a pull request, and that pull request merges itself when tests and the Canary Set pass. Deploys are pulled by the host, verified against the Canary Set before switching, and rolled back automatically if the new version does worse.

## Consequences

- Only pull requests authored by the project's own agent may auto-merge. The repo is public, and an outside pull request must never merge itself.
- The agent may add Canary URLs but never remove or loosen one without the owner's approval, so it cannot make a failure pass by weakening the test.
- Fix evidence must come from the host (the residential connection), because GitHub's runners have data-center addresses and see different blocking.
- Nothing pushes into the home network; the host only pulls.
