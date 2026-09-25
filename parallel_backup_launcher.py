"""Parallel Backup launcher.

The application and launcher intentionally share one backup engine.
Direct execution of app.py and BAT execution therefore use identical code.
"""

from app import main


if __name__ == "__main__":
    main()
