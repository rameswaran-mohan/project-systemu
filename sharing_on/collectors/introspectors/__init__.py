"""Factory for resolving the correct UI Introspector depending on the Host OS."""

import queue

from sharing_on.events.store import EventStore
from sharing_on.platform_info import OSType

def get_ui_introspector(os_type: OSType, event_store: EventStore, coordinate_queue: queue.Queue):
    """Factory method to return the platform-specific UI Introspector."""
    if os_type == OSType.WINDOWS:
        from .windows import WindowsUIIntrospector
        return WindowsUIIntrospector(event_store, coordinate_queue)
    
    elif os_type == OSType.MACOS:
        from .macos import MacOSUIIntrospector
        return MacOSUIIntrospector(event_store, coordinate_queue)
        
    elif os_type == OSType.LINUX:
        from .linux import LinuxUIIntrospector
        return LinuxUIIntrospector(event_store, coordinate_queue)
        
    return None
