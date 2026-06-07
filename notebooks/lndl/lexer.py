"""
LNDL Lexer - Clean tokenization for cognitive programming
"""

from dataclasses import dataclass
from enum import Enum, auto


class TokenType(Enum):
    # Core
    LVAR_START = auto()  # <let-lvar,
    LVAR_END = auto()  # <let-lvar/>
    FIELD = auto()  # field_context
    AS = auto()  # as
    IF = auto()  # IF
    ELSE = auto()  # ELSE
    LET = auto()  # let
    DO = auto()  # DO
    WITH = auto()  # WITH

    # Semantic ops
    SEM_OP = auto()  # similar, synthesize, etc.

    # Literals
    ID = auto()  # identifiers
    NUM = auto()  # numbers
    STR = auto()  # strings

    # Operators
    ASSIGN = auto()  # =
    GT = auto()  # >
    LT = auto()  # <
    GTE = auto()  # >=
    LTE = auto()  # <=
    EQ = auto()  # ==
    PLUS = auto()  # +
    MINUS = auto()  # -

    # Punctuation
    SEMI = auto()  # ;
    COMMA = auto()  # ,
    LPAREN = auto()  # (
    RPAREN = auto()  # )
    STAR = auto()  # *
    COLON = auto()  # :

    # Control
    NEWLINE = auto()
    EOF = auto()
    UNKNOWN = auto()

    # Markdown
    CODE_BLOCK = auto()  # ```


@dataclass
class Token:
    type: TokenType
    value: str
    line: int
    column: int


class Lexer:
    """LNDL lexer"""

    # Core semantic operations - clean names
    SEM_OPS = {
        "similar",
        "contradicts",
        "synthesize",
        "confidence",
        "complexity",
        "relevance",
        "anchor",
        "project",
    }

    # Keywords
    KEYWORDS = {
        "IF": TokenType.IF,
        "ELSE": TokenType.ELSE,
        "let": TokenType.LET,
        "DO": TokenType.DO,
        "ACTION": TokenType.WITH,  # ACTION is equivalent to WITH in DO statements
        "WITH": TokenType.WITH,
        "as": TokenType.AS,
        "return": TokenType.ID,  # return treated as identifier for now
        "if": TokenType.IF,  # lowercase if
        "else": TokenType.ELSE,  # lowercase else
    }

    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.line = 1
        self.column = 1
        self.tokens: list[Token] = []

    def current_char(self) -> str | None:
        """Get current character"""
        if self.pos >= len(self.text):
            return None
        return self.text[self.pos]

    def peek_char(self, offset: int = 1) -> str | None:
        """Peek at character ahead"""
        peek_pos = self.pos + offset
        if peek_pos >= len(self.text):
            return None
        return self.text[peek_pos]

    def advance(self):
        """Move to next character"""
        if self.pos < len(self.text) and self.text[self.pos] == "\n":
            self.line += 1
            self.column = 1
        else:
            self.column += 1
        self.pos += 1

    def skip_whitespace(self):
        """Skip whitespace except newlines"""
        while self.current_char() and self.current_char() in " \t\r":
            self.advance()

    def read_identifier(self) -> str:
        """Read identifier or keyword"""
        result = ""
        while self.current_char() and (self.current_char().isalnum() or self.current_char() in "_"):
            result += self.current_char()
            self.advance()
        return result

    def read_number(self) -> str:
        """Read number (int or float)"""
        result = ""
        while self.current_char() and (self.current_char().isdigit() or self.current_char() == "."):
            result += self.current_char()
            self.advance()
        return result

    def read_string(self) -> str:
        """Read quoted string with escape sequence handling"""
        quote_char = self.current_char()
        self.advance()  # Skip opening quote

        result = ""
        while self.current_char() and self.current_char() != quote_char:
            if self.current_char() == "\\":
                self.advance()
                if self.current_char():
                    # Handle common escape sequences
                    escape_char = self.current_char()
                    if escape_char == "n":
                        result += "\n"
                    elif escape_char == "t":
                        result += "\t"
                    elif escape_char == "r":
                        result += "\r"
                    elif escape_char == "\\":
                        result += "\\"
                    elif escape_char == '"':
                        result += '"'
                    elif escape_char == "'":
                        result += "'"
                    else:
                        # For unknown escape sequences, just add the character
                        result += escape_char
                    self.advance()
            else:
                result += self.current_char()
                self.advance()

        if self.current_char() == quote_char:
            self.advance()  # Skip closing quote

        return result

    def read_lvar_content(self) -> str:
        """Read content between <let-lvar...> and <let-lvar/>"""
        result = ""

        # Look for closing tag
        while self.pos < len(self.text):
            if self.text[self.pos : self.pos + 10] == "<let-lvar/":
                break
            result += self.current_char()
            self.advance()

        return result.strip()

    def tokenize(self) -> list[Token]:
        """Tokenize LNDL source code"""
        while self.current_char():
            # Skip whitespace
            if self.current_char() in " \t\r":
                self.skip_whitespace()
                continue

            # Newlines
            if self.current_char() == "\n":
                self.tokens.append(Token(TokenType.NEWLINE, "\n", self.line, self.column))
                self.advance()
                continue

            # Cognitive variable start: <let lvar
            if self.text[self.pos : self.pos + 9] == "<let lvar":
                start_pos = self.pos
                self.pos += 9  # Skip '<let lvar'
                token_value = self.text[start_pos : self.pos]
                self.tokens.append(
                    Token(
                        TokenType.LVAR_START,
                        token_value,
                        self.line,
                        self.column,
                    )
                )
                self.column += 9
                continue

            # Cognitive variable end: <lvar/>
            if self.text[self.pos : self.pos + 6] == "<lvar/":
                if self.pos + 6 < len(self.text) and self.text[self.pos + 6] == ">":
                    self.tokens.append(
                        Token(
                            TokenType.LVAR_END,
                            "<lvar/>",
                            self.line,
                            self.column,
                        )
                    )
                    self.pos += 7
                    self.column += 7
                    continue

            # Code blocks: ```lndl or ```
            if self.text[self.pos : self.pos + 3] == "```":
                start_pos = self.pos
                self.pos += 3  # Skip ```
                self.column += 3

                # Skip optional language identifier (like 'lndl')
                while self.current_char() and self.current_char() not in "\n\r":
                    self.advance()

                # Skip the rest - we'll just ignore code block markers
                continue

            # Identifiers and keywords
            if self.current_char().isalpha() or self.current_char() == "_":
                # Capture position before reading identifier
                start_line = self.line
                start_column = self.column
                identifier = self.read_identifier()

                # Check if it's a field identifier (starts with field_)
                if identifier.startswith("field_"):
                    token_type = TokenType.FIELD
                # Check if it's a semantic operation
                elif identifier in self.SEM_OPS:
                    token_type = TokenType.SEM_OP
                # Check if it's a keyword
                elif identifier in self.KEYWORDS:
                    token_type = self.KEYWORDS[identifier]
                else:
                    token_type = TokenType.ID

                self.tokens.append(Token(token_type, identifier, start_line, start_column))
                continue

            # Numbers
            if self.current_char().isdigit():
                # Capture position before reading number
                start_line = self.line
                start_column = self.column
                number = self.read_number()
                self.tokens.append(Token(TokenType.NUM, number, start_line, start_column))
                continue

            # Strings
            if self.current_char() in "\"'":
                # Capture position before reading string
                start_line = self.line
                start_column = self.column
                string_val = self.read_string()
                self.tokens.append(Token(TokenType.STR, string_val, start_line, start_column))
                continue

            # Single character tokens
            char = self.current_char()
            if char == ";":
                self.tokens.append(Token(TokenType.SEMI, char, self.line, self.column))
            elif char == ",":
                self.tokens.append(Token(TokenType.COMMA, char, self.line, self.column))
            elif char == "(":
                self.tokens.append(Token(TokenType.LPAREN, char, self.line, self.column))
            elif char == ")":
                self.tokens.append(Token(TokenType.RPAREN, char, self.line, self.column))
            elif char == "*":
                self.tokens.append(Token(TokenType.STAR, char, self.line, self.column))
            elif char == "=":
                if self.peek_char() == "=":
                    self.tokens.append(Token(TokenType.EQ, "==", self.line, self.column))
                    self.advance()  # Skip second =
                else:
                    self.tokens.append(Token(TokenType.ASSIGN, char, self.line, self.column))
            elif char == ">":
                if self.peek_char() == "=":
                    self.tokens.append(Token(TokenType.GTE, ">=", self.line, self.column))
                    self.advance()
                else:
                    self.tokens.append(Token(TokenType.GT, char, self.line, self.column))
            elif char == "<":
                if self.peek_char() == "=":
                    self.tokens.append(Token(TokenType.LTE, "<=", self.line, self.column))
                    self.advance()
                else:
                    self.tokens.append(Token(TokenType.LT, char, self.line, self.column))
            elif char == "+":
                self.tokens.append(Token(TokenType.PLUS, char, self.line, self.column))
            elif char == "-":
                self.tokens.append(Token(TokenType.MINUS, char, self.line, self.column))
            elif char == ":":
                self.tokens.append(Token(TokenType.COLON, char, self.line, self.column))
            else:
                # Unknown character
                self.tokens.append(Token(TokenType.UNKNOWN, char, self.line, self.column))

            self.advance()

        # Add EOF token
        self.tokens.append(Token(TokenType.EOF, "", self.line, self.column))
        return self.tokens
