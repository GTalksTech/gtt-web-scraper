"""The Incident pipeline's GitHub side, run by .github/workflows/incident.yml and
reachability.yml (not by the service). It turns an Incident into a GitHub issue that
emails Garrett, decides whether the fixing agent may run, and sends the 🙋 and ✅
notices. Standard library only, so a workflow runs it without installing anything."""
