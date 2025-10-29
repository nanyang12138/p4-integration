#!/usr/bin/env python3
"""
Lightweight verification for optimizations (no SSH dependencies)
"""

def test_constants():
    """Test constants module"""
    print("Testing constants module...")
    import sys
    import os
    sys.path.insert(0, os.path.dirname(__file__))
    
    from app.constants import JobStatus, JobStage, Defaults, JobIdFormat
    
    assert JobStatus.QUEUED == "queued"
    assert JobStatus.PUSHED == "pushed"
    assert JobStage.INTEGRATE == "integrate"
    assert Defaults.DEFAULT_P4_BIN == "p4"
    
    # Test ID generation
    readable_id = JobIdFormat.generate_readable_id("20251029", 5)
    assert readable_id == "INT-20251029-005"
    print("[OK] Constants module")

def test_env_helper():
    """Test env helper"""
    print("Testing env helper...")
    import sys
    import os
    sys.path.insert(0, os.path.dirname(__file__))
    
    from app.env_helper import EnvInitHelper
    
    # Test with enabled config
    config_enabled = {
        "env_init": {
            "enabled": True,
            "init_script": "/test/init.sh",
            "bootenv_cmd": "bootenv"
        }
    }
    helper = EnvInitHelper(config_enabled)
    assert helper.enabled == True
    assert "/test/init.sh" in helper.get_init_commands()
    
    # Test with disabled config
    config_disabled = {"env_init": {"enabled": False}}
    helper2 = EnvInitHelper(config_disabled)
    assert helper2.enabled == False
    assert helper2.get_init_commands() == ""
    
    print("[OK] Env helper")

def test_job_id():
    """Test job ID generation"""
    print("Testing job ID...")
    from datetime import datetime
    import sys
    import os
    sys.path.insert(0, os.path.dirname(__file__))
    
    from app.constants import JobIdFormat
    
    today = datetime.now().strftime('%Y%m%d')
    id1 = JobIdFormat.generate_readable_id(today, 1)
    id2 = JobIdFormat.generate_readable_id(today, 999)
    
    assert id1 == f"INT-{today}-001"
    assert id2 == f"INT-{today}-999"
    print(f"[OK] Job ID (examples: {id1}, {id2})")

if __name__ == "__main__":
    print("=" * 60)
    print("P4 Integration - Quick Validation")
    print("=" * 60)
    
    try:
        test_constants()
        test_env_helper()
        test_job_id()
        
        print("\n" + "=" * 60)
        print("[SUCCESS] All basic validations passed")
        print("=" * 60)
        print("\nOptimizations completed:")
        print("  - UI cleanup (removed unused elements)")
        print("  - Job ID improvement (INT-YYYYMMDD-NNN)")
        print("  - Changelist description formatting")
        print("  - Timestamp display (relative time)")
        print("  - Storage cache (memory + lazy write)")
        print("  - Resolve preview optimization (cache + debounce)")
        print("  - Environment init helper (DRY)")
        print("  - Logging improvements (print -> logging)")
        print("\nNext: Test by starting the server and creating a job")
    except Exception as e:
        print(f"\n[ERROR] Validation failed: {e}")
        import traceback
        traceback.print_exc()

