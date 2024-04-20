E2B_key_scheme = "E2B_API_KEY"

def set_up_interpreter(interpreter_provider="e2b",key_scheme=E2B_key_scheme):
    
    if interpreter_provider == "e2b":
        from dotenv import load_dotenv 
        load_dotenv()
        
        from lionagi.libs import SysUtil
        SysUtil.check_import("e2b_code_interpreter")
        
        from e2b_code_interpreter import CodeInterpreter
        from os import getenv

        return CodeInterpreter(api_key=getenv(key_scheme))

    else:
        raise ValueError("Invalid interpreter provider")
    

def extract_code_blocks(code):
    print("Extracting code blocks...")
    code_blocks = []
    lines = code.split('\n')
    inside_code_block = False
    current_block = []

    for line in lines:
        if line.startswith('```'):
            if inside_code_block:
                code_blocks.append('\n'.join(current_block))
                current_block = []
                inside_code_block = False
            else:
                inside_code_block = True
        elif inside_code_block:
            current_block.append(line)

    if current_block:
        code_blocks.append('\n'.join(current_block))

    print(f"Extracted {len(code_blocks)} code block(s).")
    return '\n\n'.join(code_blocks)

def install_missing_dependencies(required_libraries):
    print("Checking for missing dependencies...")
    missing_libraries = [
        library
        for library in required_libraries
        if not is_library_installed(library)
    ]

    if missing_libraries:
        print(f"Missing libraries: {', '.join(missing_libraries)}")
        for library in missing_libraries:
            print(f"Installing {library}...")
            install_library(library)
        print("Installation completed.")
    else:
        print("All required dependencies are already installed.")

def is_library_installed(library):
    try:
        import importlib
        importlib.import_module(library)
        print(f"{library} is already installed.")
        return True
    except ImportError:
        print(f"{library} is not installed.")
        return False

def install_library(library):
    try:
        import subprocess
        import sys
        
        subprocess.check_call([sys.executable, "-m", "pip", "install", library])
        print(f"Successfully installed {library}.")
    except subprocess.CalledProcessError as e:
        print(f"Error occurred while installing {library}: {str(e)}")
        print("Please check the error message and ensure you have the necessary permissions to install packages.")
        print("You may need to run the script with administrative privileges or use a virtual environment.")
