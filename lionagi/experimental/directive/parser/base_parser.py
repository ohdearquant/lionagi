from typing import List, Optional

from lionagi.libs.tokenizer.token import BaseToken
from ..schema import IfNode, TryNode, ForNode


class BaseDirectiveParser:
    """A base parser with lookahead, error recovery, and backtracking support.

    Attributes:
        tokens (List[BaseToken]): A list of tokens to be parsed.
        current_token_index (int): The index of the current token in the tokens list.
        current_token (Optional[BaseToken]): The current token being processed.

    Examples:
        >>> tokenizer = BaseTokenizer("IF x > 10 THEN DO something ENDIF")
        >>> tokens = tokenizer.get_tokens()
        >>> parser = BaseParser(tokens)
        >>> print(parser.current_token)
        BaseToken(KEYWORD, IF)
    """

    def __init__(self, tokens: List[BaseToken]):
        self.tokens = tokens
        self.current_token_index = -1
        self.current_token: Optional[BaseToken] = None
        self.next_token()

    def next_token(self) -> None:
        """Advances to the next token in the list."""
        self.current_token_index += 1
        if self.current_token_index < len(self.tokens):
            self.current_token = self.tokens[self.current_token_index]
        else:
            self.current_token = None

    def peek_next_token(self, offset: int = 1) -> BaseToken | None:
        """Peeks at the next token without consuming it.

        Args:
            offset (int): The number of tokens to look ahead.

        Returns:
            Optional[BaseToken]: The token at the specified lookahead offset, or None if end of list.
        """
        peek_index = self.current_token_index + offset
        if peek_index < len(self.tokens):
            return self.tokens[peek_index]
        else:
            return None

    def skip_until(self, token_types: List[str]) -> None:
        """Skips tokens until a token of the specified type is found.

        Args:
            token_types (List[str]): A list of token types to stop skipping.
        """
        while self.current_token and self.current_token.type not in token_types:
            self.next_token()

    def mark(self) -> int:
        """Marks the current position in the token list for potential backtracking.

        Returns:
            int: The current token index.
        """
        return self.current_token_index

    def reset_to_mark(self, mark: int) -> None:
        """Resets the parser to a previously marked position.

        Args:
            mark (int): The token index to reset to.
        """
        self.current_token_index = mark - 1
        self.next_token()

    def skip_semicolon(self):
        if self.current_token and self.current_token.value == ";":
            self.next_token()

    def parse_expression(self):
        expr = ""
        while self.current_token and self.current_token.value != ";":
            expr += self.current_token.value + " "
            self.next_token()
        # Expecting a semicolon at the end of the condition
        if self.current_token.value != ";":
            raise SyntaxError("Expected ';' at the end of the condition")
        self.next_token()  # Move past the semicolon to the next part of the statement
        return expr.strip()

    def parse_if_block(self):
        block = []
        # Parse the block until 'ELSE', 'ENDIF', ensuring not to include semicolons as part of the block
        while self.current_token and self.current_token.value not in ("ENDIF", "ELSE"):
            if self.current_token.value == "DO":
                self.next_token()  # Move past 'DO' to get to the action
            block.append(self.current_token.value)  # Add the action to the block
            self.next_token()  # Move to the next token, which could be a semicolon or the next action
            if self.current_token.value == ";":
                self.next_token()  # Move past the semicolon
        return block

    def parse_if_statement(self):
        if self.current_token.type != "KEYWORD" or self.current_token.value != "IF":
            raise SyntaxError("Expected IF statement")
        self.next_token()  # Skip 'IF'

        condition = self.parse_expression()  # Now properly ends after the semicolon

        true_block = []
        if self.current_token.value == "DO":
            true_block = self.parse_if_block()  # Parse true block after 'DO'

        false_block = None
        if self.current_token and self.current_token.value == "ELSE":
            self.next_token()  # Skip 'ELSE', expect 'DO' next for the false block
            self.skip_semicolon()
            if self.current_token.value != "DO":
                raise SyntaxError("Expected 'DO' after 'ELSE'")
            self.next_token()  # Skip 'DO'
            false_block = self.parse_if_block()  # Parse false block

        return IfNode(condition, true_block, false_block)

    def parse_for_statement(self):
        if self.current_token.type != "KEYWORD" or self.current_token.value != "FOR":
            raise SyntaxError("Expected FOR statement")
        self.next_token()  # Skip 'FOR'

        # Parse the iterator variable
        if self.current_token.type != "IDENTIFIER":
            raise SyntaxError("Expected iterator variable after FOR")
        iterator = self.current_token.value
        self.next_token()  # Move past the iterator variable

        # Expect and skip 'IN' keyword
        if self.current_token.type != "KEYWORD" or self.current_token.value != "IN":
            raise SyntaxError("Expected 'IN' after iterator variable")
        self.next_token()  # Move past 'IN'

        # Parse the collection
        if self.current_token.type not in ["IDENTIFIER", "LITERAL"]:
            raise SyntaxError("Expected collection after 'IN'")
        collection = self.current_token.value
        self.next_token()  # Move past the collection

        # Now, parse the block of statements to execute
        true_block = self.parse_for_block()

        # Construct and return a ForNode
        return ForNode(iterator, collection, true_block)

    def parse_for_block(self):
        block = []
        # Skip initial 'DO' if present
        if self.current_token and self.current_token.value == "DO":
            self.next_token()

        while self.current_token and self.current_token.value not in ("ENDFOR",):
            if self.current_token.value == ";":
                # If a semicolon is encountered, skip it and move to the next token
                self.next_token()
                continue
            # Add the current token to the block unless it's a 'DO' or ';'
            if self.current_token.value != "DO":
                block.append(self.current_token.value)
            self.next_token()

        # The loop exits when 'ENDFOR' is encountered; move past it for subsequent parsing
        self.next_token()  # Skip 'ENDFOR'
        return block

    def parse_try_statement(self):
        if self.current_token.type != "KEYWORD" or self.current_token.value != "TRY":
            raise SyntaxError("Expected TRY statement")
        self.next_token()  # Skip 'TRY'

        try_block = self.parse_try_block("EXCEPT")  # Parse the try block until 'EXCEPT'

        # Now expecting 'EXCEPT' keyword
        if not (self.current_token and self.current_token.value == "EXCEPT"):
            raise SyntaxError("Expected 'EXCEPT' after try block")
        self.next_token()  # Move past 'EXCEPT'

        except_block = self.parse_try_block(
            "ENDTRY"
        )  # Parse the except block until 'ENDTRY'

        # Ensure we are correctly positioned after 'ENDTRY'
        if self.current_token and self.current_token.value != "ENDTRY":
            raise SyntaxError("Expected 'ENDTRY' at the end of except block")
        self.next_token()  # Move past 'ENDTRY' for subsequent parsing

        return TryNode(try_block, except_block)

    def parse_try_block(self, stop_keyword):
        block = []
        while self.current_token and self.current_token.value != stop_keyword:
            if self.current_token.value == "DO":
                self.next_token()  # Move past 'DO' to get to the action
            elif self.current_token.value == ";":
                self.next_token()  # Move past the semicolon
                continue  # Skip adding ';' to the block
            else:
                block.append(self.current_token.value)  # Add the action to the block
                self.next_token()

        return block


# "IF condition1 && condition2; DO action2; ELSE; DO action3; ENDIF;"
# "FOR input_ IN collections; DO action(input_); ENDFOR;"
# "TRY; DO action(); EXCEPT; DO action(input_); ENDTRY;"
