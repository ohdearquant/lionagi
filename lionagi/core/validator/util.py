from collections.abc import Iterable, Mapping, Generator

def _validate_keys(keys):
    """
        choices can be provided from various sources:
    - mapping such as dict, their keys will be used as choices
    - iterables including list, tuple, set, generator, enum, etc.
    - strings, comma separated values
    """
    
    
    try:
        if isinstance(keys, Mapping):
            return list(keys.keys())
        
        elif isinstance(keys, (list, tuple, set, Generator)):
            return set(keys)
        
        elif isinstance(keys, str):
            if "," in keys:
                return list({i.strip() for i in keys.split(",")})
            return [keys.strip()]
        
        keys = [i.value for i in keys]
    
    except Exception as e:
        raise ValueError(f"invalid choices {keys}") from e
    
    return keys
    