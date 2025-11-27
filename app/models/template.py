"""
Template Model - Job Template Management

Handles storage and retrieval of job templates.
Templates can be global (shared by all users) or private (per-user).
"""
import os
import uuid
import yaml
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any

from app.config import get_workspace_data_dir, get_global_templates_dir

logger = logging.getLogger("TemplateManager")


class TemplateManager:
    """Manages job templates - both global and private."""
    
    def __init__(self):
        self.global_templates_dir = get_global_templates_dir()
    
    def _get_user_templates_dir(self, workspace: str, username: str) -> str:
        """Get directory for user's private templates in a workspace."""
        ws_data_dir = get_workspace_data_dir(workspace)
        user_dir = os.path.join(ws_data_dir, "templates", username)
        os.makedirs(user_dir, exist_ok=True)
        return user_dir
    
    def _load_template_file(self, filepath: str) -> Optional[Dict[str, Any]]:
        """Load a template from a YAML file."""
        try:
            if not os.path.exists(filepath):
                return None
            with open(filepath, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logger.error(f"Failed to load template from {filepath}: {e}")
        return None
    
    def _save_template_file(self, filepath: str, template: Dict[str, Any]) -> bool:
        """Save a template to a YAML file."""
        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with open(filepath, 'w', encoding='utf-8') as f:
                yaml.dump(template, f, default_flow_style=False, allow_unicode=True)
            return True
        except Exception as e:
            logger.error(f"Failed to save template to {filepath}: {e}")
            return False
    
    def create_template(
        self,
        name: str,
        config: Dict[str, Any],
        owner: str,
        template_type: str = "private",
        workspace: str = None
    ) -> Optional[Dict[str, Any]]:
        """Create a new template.
        
        Args:
            name: Template name
            config: Template configuration (p4, ssh, branch_spec, etc.)
            owner: Username of the template creator
            template_type: "global" or "private"
            workspace: Required for private templates
            
        Returns:
            Created template dict or None on failure
        """
        template_id = f"tpl-{uuid.uuid4().hex[:12]}"
        
        template = {
            "id": template_id,
            "name": name,
            "type": template_type,
            "owner": owner,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "config": config
        }
        
        # Determine save location
        if template_type == "global":
            filepath = os.path.join(self.global_templates_dir, f"{template_id}.yaml")
        else:
            if not workspace:
                logger.error("workspace is required for private templates")
                return None
            user_dir = self._get_user_templates_dir(workspace, owner)
            filepath = os.path.join(user_dir, f"{template_id}.yaml")
        
        if self._save_template_file(filepath, template):
            logger.info(f"Created template {template_id}: {name} (type={template_type}, owner={owner})")
            return template
        return None
    
    def get_template(self, template_id: str, workspace: str = None, username: str = None) -> Optional[Dict[str, Any]]:
        """Get a template by ID.
        
        Searches in global templates first, then user's private templates.
        
        Args:
            template_id: Template ID
            workspace: Workspace path for private template lookup
            username: Username for private template lookup
        """
        # Check global templates first
        global_path = os.path.join(self.global_templates_dir, f"{template_id}.yaml")
        template = self._load_template_file(global_path)
        if template:
            return template
        
        # Check private templates if workspace and username provided
        if workspace and username:
            user_dir = self._get_user_templates_dir(workspace, username)
            private_path = os.path.join(user_dir, f"{template_id}.yaml")
            template = self._load_template_file(private_path)
            if template:
                return template
        
        return None
    
    def list_global_templates(self) -> List[Dict[str, Any]]:
        """List all global templates."""
        templates = []
        try:
            if os.path.exists(self.global_templates_dir):
                for filename in os.listdir(self.global_templates_dir):
                    if filename.endswith('.yaml'):
                        filepath = os.path.join(self.global_templates_dir, filename)
                        template = self._load_template_file(filepath)
                        if template:
                            templates.append(template)
        except Exception as e:
            logger.error(f"Failed to list global templates: {e}")
        return templates
    
    def list_user_templates(self, workspace: str, username: str) -> List[Dict[str, Any]]:
        """List all private templates for a user in a workspace."""
        templates = []
        try:
            user_dir = self._get_user_templates_dir(workspace, username)
            if os.path.exists(user_dir):
                for filename in os.listdir(user_dir):
                    if filename.endswith('.yaml'):
                        filepath = os.path.join(user_dir, filename)
                        template = self._load_template_file(filepath)
                        if template:
                            templates.append(template)
        except Exception as e:
            logger.error(f"Failed to list user templates: {e}")
        return templates
    
    def list_all_templates(self, workspace: str = None, username: str = None) -> List[Dict[str, Any]]:
        """List all templates available to a user.
        
        Returns global templates + user's private templates.
        """
        templates = self.list_global_templates()
        
        if workspace and username:
            user_templates = self.list_user_templates(workspace, username)
            templates.extend(user_templates)
        
        # Sort by name
        templates.sort(key=lambda t: t.get('name', '').lower())
        return templates
    
    def update_template(
        self,
        template_id: str,
        updates: Dict[str, Any],
        workspace: str = None,
        username: str = None
    ) -> Optional[Dict[str, Any]]:
        """Update an existing template.
        
        Args:
            template_id: Template ID to update
            updates: Dict with fields to update (name, config)
            workspace: Workspace path for private templates
            username: Username for permission check
        """
        template = self.get_template(template_id, workspace, username)
        if not template:
            return None
        
        # Check permissions
        if template.get('owner') != username:
            logger.warning(f"User {username} cannot update template {template_id} owned by {template.get('owner')}")
            return None
        
        # Apply updates
        if 'name' in updates:
            template['name'] = updates['name']
        if 'config' in updates:
            template['config'] = updates['config']
        template['updated_at'] = datetime.now().isoformat()
        
        # Determine save location
        if template.get('type') == 'global':
            filepath = os.path.join(self.global_templates_dir, f"{template_id}.yaml")
        else:
            if not workspace:
                return None
            user_dir = self._get_user_templates_dir(workspace, template.get('owner', username))
            filepath = os.path.join(user_dir, f"{template_id}.yaml")
        
        if self._save_template_file(filepath, template):
            logger.info(f"Updated template {template_id}")
            return template
        return None
    
    def delete_template(
        self,
        template_id: str,
        workspace: str = None,
        username: str = None
    ) -> bool:
        """Delete a template.
        
        Args:
            template_id: Template ID to delete
            workspace: Workspace path for private templates
            username: Username for permission check
        """
        template = self.get_template(template_id, workspace, username)
        if not template:
            return False
        
        # Check permissions
        if template.get('owner') != username:
            logger.warning(f"User {username} cannot delete template {template_id} owned by {template.get('owner')}")
            return False
        
        # Determine file location
        if template.get('type') == 'global':
            filepath = os.path.join(self.global_templates_dir, f"{template_id}.yaml")
        else:
            if not workspace:
                return False
            user_dir = self._get_user_templates_dir(workspace, template.get('owner', username))
            filepath = os.path.join(user_dir, f"{template_id}.yaml")
        
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                logger.info(f"Deleted template {template_id}")
                return True
        except Exception as e:
            logger.error(f"Failed to delete template {template_id}: {e}")
        
        return False


# Global instance
template_manager = TemplateManager()

