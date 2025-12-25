import sys
from pathlib import Path

# Add project root to path for imports
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Re-export from common config for backward compatibility
from config import DB_CONFIG

__all__ = ['DB_CONFIG']

