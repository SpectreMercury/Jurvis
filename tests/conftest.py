"""Tests never read the developer's own files: the skills folder shared with Claude Code is switched off."""
import os

os.environ["JURVIS_SHARED_SKILLS"] = ""
