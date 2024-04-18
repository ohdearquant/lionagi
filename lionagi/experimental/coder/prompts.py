"""
Prompts for the coder
mostly from rUv: https://gist.github.com/ruvnet/4b41ee8eaabd6e72cf18b6352437c738
- guidance_response_: guidance response for the coder
- write_codes
- review_codes
- modify_codes
- debug_codes
"""

guidance_response_ = """
    Guidance from super intelligent code bot:
    {guidance_response}
    Please generate a Python function that satisfies the prompt and follows the provided guidance, while adhering to these coding standards:
    - Use descriptive and meaningful names for variables, functions, and classes.
    - Follow the naming conventions: lowercase with underscores for functions and variables, CamelCase for classes.
    - Keep functions small and focused, doing one thing well.
    - Use 4 spaces for indentation, and avoid mixing spaces and tabs.
    - Limit line length to 79 characters for better readability.
    - Use docstrings to document functions, classes, and modules, describing their purpose, parameters, and return values.
    - Use comments sparingly, and prefer descriptive names and clear code structure over comments.
    - Handle exceptions appropriately and raise exceptions with clear error messages.
    - Use blank lines to separate logical sections of code, but avoid excessive blank lines.
    - Import modules in a specific order: standard library, third-party, and local imports, separated by blank lines.
    - Use consistent quotes (single or double) for strings throughout the codebase.
    - Follow the PEP 8 style guide for more detailed coding standards and best practices.
"""

_write_prompt = "Please write a Python function that satisfies the prompt and follows the provided guidance."
_review_prompt = "Please review the following code and remove any unnecessary markdown or descriptions:\n\n{code}\n"
_modify_prompt = """
Please generate updated code based on the previous code and the additional request. 
 ### Previous code: \n\n{code}\n
 ### Additional request: \n\n{additional_request}\n
"""
_debug_prompt = """
please debug the code, fix the error and provide the correctly updated code to satisfy the prompt according to the guidance provided.
 ### code: \n\n {code}\n , ran into the following 
 ### error: \n\n {error}\n
"""

coder_prompts = {
    "guidance_response": guidance_response_,
    "write_code": _write_prompt,
    "review_code": _review_prompt,
    "modify_code": _modify_prompt,
    "debug_code": _debug_prompt,
}
