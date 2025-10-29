"""
Environment initialization helper for P4 operations

Centralizes the duplicated environment initialization logic across p4_client.py and jobs.py
"""
from typing import Dict, Any
from .constants import Defaults


class EnvInitHelper:
    """Helper class to manage environment initialization scripts and commands."""
    
    def __init__(self, config: Dict[str, Any]):
        self._config = config
        env_init_cfg = config.get("env_init", {})
        self.enabled = env_init_cfg.get("enabled", True) if isinstance(env_init_cfg, dict) else True
        
        if isinstance(env_init_cfg, dict):
            self.init_script = env_init_cfg.get("init_script", Defaults.DEFAULT_INIT_SCRIPT)
            self.bootenv_cmd = env_init_cfg.get("bootenv_cmd", Defaults.DEFAULT_BOOTENV_CMD)
            self.name_check_tool = env_init_cfg.get("name_check_tool", Defaults.DEFAULT_NAME_CHECK_TOOL)
        else:
            self.init_script = Defaults.DEFAULT_INIT_SCRIPT
            self.bootenv_cmd = Defaults.DEFAULT_BOOTENV_CMD
            self.name_check_tool = Defaults.DEFAULT_NAME_CHECK_TOOL
    
    def get_init_commands(self) -> str:
        """Get shell commands for environment initialization.
        
        Returns:
            Empty string if disabled, otherwise the initialization commands.
        """
        if not self.enabled:
            return ""
        
        return f"source {self.init_script} >/dev/null 2>&1 || true; {self.bootenv_cmd} >/dev/null 2>&1 || true; "
    
    def get_init_commands_multiline(self) -> str:
        """Get multi-line format for bash scripts.
        
        Returns:
            Empty string or comment if disabled, otherwise multi-line commands.
        """
        if not self.enabled:
            return "# Environment initialization skipped"
        
        return f"""source {self.init_script} >/dev/null 2>&1 || true
{self.bootenv_cmd} >/dev/null 2>&1 || true"""
    
    def get_name_check_tool_path(self) -> str:
        """Get the path to name_check tool."""
        return self.name_check_tool

