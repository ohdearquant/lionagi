from collections.abc import Callable
import re
import inspect
import itertools
import contextlib
from functools import singledispatchmethod
from typing import Any
import numpy as np
import lionagi.libs.ln_convert as convert


md_json_char_map = {"\n": "\\n", "\r": "\\r", "\t": "\\t", '"': '\\"'}


class ParseUtil:

    @staticmethod
    def fuzzy_parse_json(str_to_parse: str, *, strict: bool = False):
        """
        Parses a potentially incomplete or malformed JSON string by adding missing closing brackets or braces.

        Tries to parse the input string as JSON and, on failure due to formatting errors, attempts to correct
        the string by appending necessary closing characters before retrying.

        Args:
                s (str): The JSON string to parse.
                strict (bool, optional): If True, enforces strict JSON syntax. Defaults to False.

        Returns:
                The parsed JSON object, typically a dictionary or list.

        Raises:
                ValueError: If parsing fails even after attempting to correct the string.

        Example:
                >>> fuzzy_parse_json('{"name": "John", "age": 30, "city": "New York"')
                {'name': 'John', 'age': 30, 'city': 'New York'}
        """
        try:
            return convert.to_dict(str_to_parse, strict=strict)
        except Exception:
            fixed_s = ParseUtil.fix_json_string(str_to_parse)
            try:
                return convert.to_dict(fixed_s, strict=strict)

            except Exception:
                try:
                    fixed_s = fixed_s.replace("'", '"')
                    return convert.to_dict(fixed_s, strict=strict)

                except Exception as e:
                    raise ValueError(
                        f"Failed to parse JSON even after fixing attempts: {e}"
                    ) from e

    @staticmethod
    def fix_json_string(str_to_parse: str) -> str:

        brackets = {"{": "}", "[": "]"}
        open_brackets = []

        for char in str_to_parse:
            if char in brackets:
                open_brackets.append(brackets[char])
            elif char in brackets.values():
                if not open_brackets or open_brackets[-1] != char:
                    raise ValueError("Mismatched or extra closing bracket found.")
                open_brackets.pop()

        return str_to_parse + "".join(reversed(open_brackets))

    @staticmethod
    def escape_chars_in_json(value: str, char_map=None) -> str:
        """
        Escapes special characters in a JSON string using a specified character map.

        This method replaces newline, carriage return, tab, and double quote characters
        in a given string with their escaped versions defined in the character map. If no map is provided,
        a default mapping is used.

        Args:
                value: The string to be escaped.
                char_map: An optional dictionary mapping characters to their escaped versions.
                        If not provided, a default mapping that escapes newlines, carriage returns,
                        tabs, and double quotes is used.

        Returns:
                The escaped JSON string.

        Examples:
                >>> escape_chars_in_json('Line 1\nLine 2')
                'Line 1\\nLine 2'
        """

        def replacement(match):
            char = match.group(0)
            _char_map = char_map or md_json_char_map
            return _char_map.get(char, char)  # Default to the char itself if not in map

        # Match any of the special characters to be escaped.
        return re.sub(r'[\n\r\t"]', replacement, value)

    # inspired by langchain_core.output_parsers.json (MIT License)
    # https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/output_parsers/json.py
    @staticmethod
    def extract_code_block(
        str_to_parse: str,
        language: str | None = None,
        regex_pattern: str | None = None,
        *,
        parser: Callable[[str], Any],
    ) -> Any:
        """
        Extracts and parses a code block from Markdown content.

        This method searches for a code block in the given Markdown string, optionally
        filtered by language. If a code block is found, it is parsed using the provided parser function.

        Args:
                str_to_parse: The Markdown content to search.
                language: An optional language specifier for the code block. If provided,
                        only code blocks of this language are considered.
                regex_pattern: An optional regular expression pattern to use for finding the code block.
                        If provided, it overrides the language parameter.
                parser: A function to parse the extracted code block string.

        Returns:
                The result of parsing the code block with the provided parser function.

        Raises:
                ValueError: If no code block is found in the Markdown content.

        Examples:
                >>> extract_code_block('```python\\nprint("Hello, world!")\\n```', language='python', parser=lambda x: x)
                'print("Hello, world!")'
        """

        if language:
            regex_pattern = rf"```{language}\n?(.*?)\n?```"
        else:
            regex_pattern = r"```\n?(.*?)\n?```"

        match = re.search(regex_pattern, str_to_parse, re.DOTALL)
        code_str = ""
        if match:
            code_str = match[1].strip()
        else:
            raise ValueError(
                f"No {language or 'specified'} code block found in the Markdown content."
            )

        return parser(code_str)

    @staticmethod
    def md_to_json(
        str_to_parse: str,
        *,
        expected_keys: list[str] | None = None,
        parser: Callable[[str], Any] | None = None,
    ) -> Any:
        """
        Extracts a JSON code block from Markdown content, parses it, and verifies required keys.

        This method uses `extract_code_block` to find and parse a JSON code block within the given
        Markdown string. It then optionally verifies that the parsed JSON object contains all expected keys.

        Args:
                str_to_parse: The Markdown content to parse.
                expected_keys: An optional list of keys expected to be present in the parsed JSON object.
                parser: An optional function to parse the extracted code block. If not provided,
                        `fuzzy_parse_json` is used with default settings.

        Returns:
                The parsed JSON object from the Markdown content.

        Raises:
                ValueError: If the JSON code block is missing, or if any of the expected keys are missing
                        from the parsed JSON object.

        Examples:
                >>> md_to_json('```json\\n{"key": "value"}\\n```', expected_keys=['key'])
                {'key': 'value'}
        """
        json_obj = ParseUtil.extract_code_block(
            str_to_parse, language="json", parser=parser or ParseUtil.fuzzy_parse_json
        )

        if expected_keys:
            if missing_keys := [key for key in expected_keys if key not in json_obj]:
                raise ValueError(
                    f"Missing expected keys in JSON object: {', '.join(missing_keys)}"
                )

        return json_obj

    @staticmethod
    def _extract_docstring_details_google(func):
        """
        Extracts the function description and parameter descriptions from the
        docstring following the Google style format.

        Args:
                func (Callable): The function from which to extract docstring details.

        Returns:
                Tuple[str, Dict[str, str]]: A tuple containing the function description
                and a dictionary with parameter names as keys and their descriptions as values.

        Examples:
                >>> def example_function(param1: int, param2: str):
                ...     '''Example function.
                ...
                ...     Args:
                ...         param1 (int): The first parameter.
                ...         param2 (str): The second parameter.
                ...     '''
                ...     pass
                >>> description, params = _extract_docstring_details_google(example_function)
                >>> description
                'Example function.'
                >>> params == {'param1': 'The first parameter.', 'param2': 'The second parameter.'}
                True
        """
        docstring = inspect.getdoc(func)
        if not docstring:
            return "No description available.", {}
        lines = docstring.split("\n")
        func_description = lines[0].strip()

        lines_len = len(lines)

        params_description = {}
        param_start_pos = next(
            (
                i + 1
                for i in range(1, lines_len)
                if (
                    lines[i].startswith("Args")
                    or lines[i].startswith("Arguments")
                    or lines[i].startswith("Parameters")
                )
            ),
            0,
        )
        current_param = None
        for i in range(param_start_pos, lines_len):
            if lines[i] == "":
                continue
            elif lines[i].startswith(" "):
                param_desc = lines[i].split(":", 1)
                if len(param_desc) == 1:
                    params_description[current_param] += f" {param_desc[0].strip()}"
                    continue
                param, desc = param_desc
                param = param.split("(")[0].strip()
                params_description[param] = desc.strip()
                current_param = param
            else:
                break
        return func_description, params_description

    @staticmethod
    def _extract_docstring_details_rest(func):
        """
        Extracts the function description and parameter descriptions from the
        docstring following the reStructuredText (reST) style format.

        Args:
                func (Callable): The function from which to extract docstring details.

        Returns:
                Tuple[str, Dict[str, str]]: A tuple containing the function description
                and a dictionary with parameter names as keys and their descriptions as values.

        Examples:
                >>> def example_function(param1: int, param2: str):
                ...     '''Example function.
                ...
                ...     :param param1: The first parameter.
                ...     :type param1: int
                ...     :param param2: The second parameter.
                ...     :type param2: str
                ...     '''
                ...     pass
                >>> description, params = _extract_docstring_details_rest(example_function)
                >>> description
                'Example function.'
                >>> params == {'param1': 'The first parameter.', 'param2': 'The second parameter.'}
                True
        """
        docstring = inspect.getdoc(func)
        if not docstring:
            return "No description available.", {}
        lines = docstring.split("\n")
        func_description = lines[0].strip()

        params_description = {}
        current_param = None
        for line in lines[1:]:
            line = line.strip()
            if line.startswith(":param"):
                param_desc = line.split(":", 2)
                _, param, desc = param_desc
                param = param.split()[-1].strip()
                params_description[param] = desc.strip()
                current_param = param
            elif line.startswith(" "):
                params_description[current_param] += f" {line}"

        return func_description, params_description

    @staticmethod
    def _extract_docstring_details(func, style="google"):
        """
        Extracts the function description and parameter descriptions from the
        docstring of the given function using either Google style or reStructuredText
        (reST) style format.

        Args:
                func (Callable): The function from which to extract docstring details.
                style (str): The style of docstring to parse ('google' or 'reST').

        Returns:
                Tuple[str, Dict[str, str]]: A tuple containing the function description
                and a dictionary with parameter names as keys and their descriptions as values.

        Raises:
                ValueError: If an unsupported style is provided.

        Examples:
                >>> def example_function(param1: int, param2: str):
                ...     '''Example function.
                ...
                ...     Args:
                ...         param1 (int): The first parameter.
                ...         param2 (str): The second parameter.
                ...     '''
                ...     pass
                >>> description, params = _extract_docstring_details(example_function, style='google')
                >>> description
                'Example function.'
                >>> params == {'param1': 'The first parameter.', 'param2': 'The second parameter.'}
                True
        """
        if style == "google":
            func_description, params_description = (
                ParseUtil._extract_docstring_details_google(func)
            )
        elif style == "reST":
            func_description, params_description = (
                ParseUtil._extract_docstring_details_rest(func)
            )
        else:
            raise ValueError(
                f'{style} is not supported. Please choose either "google" or "reST".'
            )
        return func_description, params_description

    @staticmethod
    def _python_to_json_type(py_type):
        """
        Converts a Python type to its JSON type equivalent.

        Args:
                py_type (str): The name of the Python type.

        Returns:
                str: The corresponding JSON type.

        Examples:
                >>> _python_to_json_type('str')
                'string'
                >>> _python_to_json_type('int')
                'number'
        """
        type_mapping = {
            "str": "string",
            "int": "number",
            "float": "number",
            "list": "array",
            "tuple": "array",
            "bool": "boolean",
            "dict": "object",
        }
        return type_mapping.get(py_type, "object")

    @staticmethod
    def _func_to_schema(func, style="google"):
        """
        Generates a schema description for a given function, using typing hints and
        docstrings. The schema includes the function's name, description, and parameters.

        Args:
                func (Callable): The function to generate a schema for.
                style (str): The docstring format ('google' or 'reST').

        Returns:
                Dict[str, Any]: A schema describing the function.

        Examples:
                >>> def example_function(param1: int, param2: str) -> bool:
                ...     '''Example function.
                ...
                ...     Args:
                ...         param1 (int): The first parameter.
                ...         param2 (str): The second parameter.
                ...     '''
                ...     return True
                >>> schema = _func_to_schema(example_function)
                >>> schema['function']['name']
                'example_function'
        """
        # Extracting function name and docstring details
        func_name = func.__name__
        func_description, params_description = ParseUtil._extract_docstring_details(
            func, style
        )

        # Extracting parameters with typing hints
        sig = inspect.signature(func)
        parameters = {
            "type": "object",
            "properties": {},
            "required": [],
        }

        for name, param in sig.parameters.items():
            # Default type to string and update if type hint is available
            param_type = "string"
            if param.annotation is not inspect.Parameter.empty:
                param_type = ParseUtil._python_to_json_type(param.annotation.__name__)

            # Extract parameter description from docstring, if available
            param_description = params_description.get(
                name, "No description available."
            )

            # Assuming all parameters are required for simplicity
            parameters["required"].append(name)
            parameters["properties"][name] = {
                "type": param_type,
                "description": param_description,
            }

        return {
            "type": "function",
            "function": {
                "name": func_name,
                "description": func_description,
                "parameters": parameters,
            },
        }

    @staticmethod
    def get_class_code(class_):
        """return the source codes for a class object"""
        if class_ is None:
            return

        import inspect, sys

        def new_getfile(object, _old_getfile=inspect.getfile):
            if not inspect.isclass(object):
                return _old_getfile(object)

            # Lookup by parent module (as in current inspect)
            if hasattr(object, "__module__"):
                object_ = sys.modules.get(object.__module__)
                if hasattr(object_, "__file__"):
                    return object_.__file__

            # If parent module is __main__, lookup by methods (NEW)
            for name, member in inspect.getmembers(object):
                if (
                    inspect.isfunction(member)
                    and object.__qualname__ + "." + member.__name__
                    == member.__qualname__
                ):
                    return inspect.getfile(member)
            else:
                raise TypeError("Source for {!r} not found".format(object))

        inspect.getfile = new_getfile

        import inspect
        from IPython.core.magics.code import extract_symbols

        obj = class_.__class__
        cell_code = "".join(inspect.linecache.getlines(new_getfile(obj)))
        class_code = extract_symbols(cell_code, obj.__name__)[0][0]
        return class_code


class StringMatch:

    @staticmethod
    def jaro_distance(s, t):
        """
        Calculate the Jaro distance between two strings.

        The Jaro distance is a measure of similarity between two strings. The higher the Jaro distance,
        the more similar the two strings are. The score is normalized such that 0 equates to no similarity
        and 1 is an exact match.

        Args:
                s: The first string to compare.
                t: The second string to compare.

        Returns:
                A float representing the Jaro distance between the two strings, ranging from 0 to 1,
                where 1 means the strings are identical.

        Examples:
                >>> jaro_distance("martha", "marhta")
                0.9444444444444445
        """
        s_len = len(s)
        t_len = len(t)
        if s_len == 0 and t_len == 0:
            return 1.0

        match_distance = (max(s_len, t_len) // 2) - 1
        s_matches = [False] * s_len
        t_matches = [False] * t_len

        matches = 0
        transpositions = 0

        for i in range(s_len):
            start = max(0, i - match_distance)
            end = min(i + match_distance + 1, t_len)

            for j in range(start, end):
                if t_matches[j]:
                    continue
                if s[i] != t[j]:
                    continue
                s_matches[i] = t_matches[j] = True
                matches += 1
                break

        if matches == 0:
            return 0.0

        k = 0
        for i in range(s_len):
            if not s_matches[i]:
                continue
            while not t_matches[k]:
                k += 1
            if s[i] != t[k]:
                transpositions += 1
            k += 1

        transpositions //= 2
        return (
            matches / s_len + matches / t_len + (matches - transpositions) / matches
        ) / 3.0

    @staticmethod
    def jaro_winkler_similarity(s, t, scaling=0.1):
        """
        Calculate the Jaro-Winkler similarity between two strings.

        The Jaro-Winkler similarity is an extension of the Jaro similarity that gives more weight to strings
        that match from the beginning for a set prefix length. This method works well for short strings such as
        person names, and is designed to improve the scoring of strings that have a common prefix.

        Args:
                s: The first string to compare.
                t: The second string to compare.
                scaling: The scaling factor for how much the score is adjusted upwards for having common prefixes.
                                 The scaling factor should be less than 1, and a typical value is 0.1.

        Returns:
                A float representing the Jaro-Winkler similarity between the two strings, ranging from 0 to 1,
                where 1 means the strings are identical.

        Examples:
                >>> jaro_winkler_similarity("dixon", "dicksonx")
                0.8133333333333332
        """
        jaro_sim = StringMatch.jaro_distance(s, t)
        prefix_len = 0
        for s_char, t_char in zip(s, t):
            if s_char == t_char:
                prefix_len += 1
            else:
                break
            if prefix_len == 4:
                break
        return jaro_sim + (prefix_len * scaling * (1 - jaro_sim))

    @staticmethod
    def levenshtein_distance(a, b):
        """
        Calculate the Levenshtein distance between two strings.

        The Levenshtein distance is a string metric for measuring the difference between two sequences.
        It is calculated as the minimum number of single-character edits (insertions, deletions, or substitutions)
        required to change one word into the other. Each operation has an equal cost.

        Args:
                a: The first string to compare.
                b: The second string to compare.

        Returns:
                An integer representing the Levenshtein distance between the two strings.

        Examples:
                >>> levenshtein_distance("kitten", "sitting")
                3
        """
        m, n = len(a), len(b)
        # Initialize 2D array (m+1) x (n+1)
        d = [[0] * (n + 1) for _ in range(m + 1)]

        # Populate the base case values
        for i in range(m + 1):
            d[i][0] = i
        for j in range(n + 1):
            d[0][j] = j

        # Compute the distance
        for i, j in itertools.product(range(1, m + 1), range(1, n + 1)):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(
                d[i - 1][j] + 1,  # deletion
                d[i][j - 1] + 1,  # insertion
                d[i - 1][j - 1] + cost,
            )  # substitution
        return d[m][n]

    @staticmethod
    def correct_dict_keys(keys: dict | list[str], dict_, score_func=None):
        if score_func is None:
            score_func = StringMatch.jaro_winkler_similarity

        fields_set = set(keys if isinstance(keys, list) else keys.keys())
        corrected_out = {}
        used_keys = set()

        for k, v in dict_.items():
            if k in fields_set:
                corrected_out[k] = v
                fields_set.remove(k)  # Remove the matched key
                used_keys.add(k)
            else:
                # Calculate Jaro-Winkler similarity scores for each potential match
                scores = np.array([score_func(k, field) for field in fields_set])
                # Find the index of the highest score
                max_score_index = np.argmax(scores)
                # Select the best match based on the highest score
                best_match = list(fields_set)[max_score_index]

                corrected_out[best_match] = v
                fields_set.remove(best_match)  # Remove the matched key
                used_keys.add(best_match)

        if len(used_keys) < len(dict_):
            for k, v in dict_.items():
                if k not in used_keys:
                    corrected_out[k] = v

        return corrected_out

    @staticmethod
    def choose_most_similar(word, correct_words_list, score_func=None):

        if score_func is None:
            score_func = StringMatch.jaro_winkler_similarity

        # Calculate Jaro-Winkler similarity scores for each potential match
        scores = np.array(
            [
                score_func(convert.to_str(word), convert.to_str(correct_word))
                for correct_word in correct_words_list
            ]
        )
        # Find the index of the highest score
        max_score_index = np.argmax(scores)
        return correct_words_list[max_score_index]

    @staticmethod
    def force_validate_dict(x, keys: dict | list[str]) -> dict:
        out_ = x

        if isinstance(out_, str):
            # first try to parse it straight as a fuzzy json
            try:
                out_ = ParseUtil.fuzzy_parse_json(out_)
            except Exception:
                try:
                    # if failed we try to extract the json block and parse it
                    out_ = ParseUtil.md_to_json(out_)
                except Exception:
                    # if still failed we try to extract the json block using re and parse it again
                    match = re.search(r"```json\n({.*?})\n```", out_, re.DOTALL)
                    if match:
                        out_ = match.group(1)
                        try:
                            out_ = ParseUtil.fuzzy_parse_json(out_)
                        except:
                            try:
                                out_ = ParseUtil.fuzzy_parse_json(
                                    out_.replace("'", '"')
                                )
                            except:
                                pass

        if isinstance(out_, dict):
            try:
                return StringMatch.correct_dict_keys(keys, out_)
            except Exception as e:
                raise ValueError(f"Failed to force_validate_dict for input: {x}") from e
