# The fixing agent's pull requests are merged by our own workflow, not GitHub's auto-merge

ADR 0002 has the agent's fixes merge themselves once the tests pass. GitHub's own auto-merge would do that, but a merge it makes with a workflow's token starts no other workflow: the image wouldn't build, so the fix would never reach pi5, and the Incident's ✅ wouldn't go out. Making it start them needs a personal access token with write access to the repo, kept as a secret that expires and must be renewed. Instead, `.github/workflows/automerge.yml` runs each time the `tests` workflow passes on a pull request, and merges it itself when it is the agent's (claude[bot], from a branch of this repo, into main) and doesn't weaken the checks; it then starts the image build and sends the ✅ directly. GitHub's auto-merge setting stays off, and branch protection on main still requires the `tests` check from GitHub Actions, so the workflow can't merge a pull request whose tests failed.

## Consequences

- No extra secret: the workflow's own token merges, starts the image build and edits the Incident.
- The workflow runs main's code, never the pull request's, and reads the pull request's files through the API as data, so a pull request can't change the rules it is judged by. Changes to `.github/` or the auto-merge code are themselves held for Garrett.
- The agent's identity is the Claude GitHub app's login, which any repository can install, so a pull request from a fork never merges, whoever opened it.
- A held fix is labelled `ready-for-human` and its Incident turns 🙋 with the reasons; Garrett merges or closes it by hand.
- What counts as weakening is listed in `src/scraper/automerge/policy.py`. A logic change to the Content Gate isn't held: the corpus must still pass.
