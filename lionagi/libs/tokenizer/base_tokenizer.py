import re
from .token import BaseToken

class BaseTokenizer:
    TOKEN_TYPES = {
        "KEYWORD": r"\b(BEGIN|END|IF|ELSE|FOR|IN|TRY|EXCEPT|ENDIF|ENDFOR|ENDTRY|DO)\b",
        "OPERATOR": r"(==|!=|>=|<=|>|<|&&|\|\||!)",
        "FUNCTION_CALL": r"\b[a-zA-Z_][a-zA-Z0-9_]*\b\((.*?)\)",
        "LITERAL": r'(\d+|\'.*?\'|".*?")',
        "IDENTIFIER": r"\b[a-zA-Z_][a-zA-Z0-9_]*\b",
        "PUNCTUATION": r"(;|,|\(|\))",
        "WHITESPACE": r"\s+",
    }

    def __init__(self, script):
        self.script = script
        self.tokens = []
        self.tokenize()

    @property
    def is_empty(self):
        return self.tokens == []

    def tokenize(self):
        position = 0
        while position < len(self.script):
            match = None
            for type_, pattern in self.TOKEN_TYPES.items():
                regex = re.compile(pattern)
                match = regex.match(self.script, position)
                if match:
                    if type_ != "WHITESPACE":  # Ignore whitespace
                        token = BaseToken(type_, match.group())
                        self.tokens.append(token)
                    position = match.end()  # Move past the matched token
                    break
            if not match:  # No match found, unrecognized token
                raise SyntaxError(f"Unexpected character: {self.script[position]}")
                # break

    def get_tokens(self):
        if self.is_empty:
            try:
                self.tokenize()
            except SyntaxError as e:
                print(e)
                return []
        return self.tokens
