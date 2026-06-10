"""
LNDL Parser - Parse tokens into Abstract Syntax Tree
"""

from .ast_nodes import *
from .lexer import Lexer, Token, TokenType


class ParseError(Exception):
    """Parser error"""

    def __init__(self, message: str, token: Token):
        self.message = message
        self.token = token
        super().__init__(f"Parse error at line {token.line}, column {token.column}: {message}")


class Parser:
    """Parser for LNDL cognitive programming language"""

    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0

    def current_token(self) -> Token:
        """Get current token"""
        if self.pos >= len(self.tokens):
            return self.tokens[-1]  # EOF token
        return self.tokens[self.pos]

    def peek_token(self, offset: int = 1) -> Token:
        """Peek at token ahead"""
        peek_pos = self.pos + offset
        if peek_pos >= len(self.tokens):
            return self.tokens[-1]  # EOF token
        return self.tokens[peek_pos]

    def advance(self):
        """Move to next token"""
        if self.pos < len(self.tokens) - 1:
            self.pos += 1

    def expect(self, token_type: TokenType) -> Token:
        """Expect specific token type"""
        token = self.current_token()
        if token.type != token_type:
            raise ParseError(f"Expected {token_type}, got {token.type}", token)
        self.advance()
        return token

    def match(self, *token_types: TokenType) -> bool:
        """Check if current token matches any of the given types"""
        return self.current_token().type in token_types

    def skip_newlines(self):
        """Skip newline tokens"""
        while self.match(TokenType.NEWLINE):
            self.advance()

    def parse(self) -> Program:
        """Parse tokens into AST"""
        statements = []

        while not self.match(TokenType.EOF):
            self.skip_newlines()

            if self.match(TokenType.EOF):
                break

            stmt = self.parse_statement()
            if stmt:
                statements.append(stmt)

        return Program(statements)

    def parse_statement(self) -> Stmt | None:
        """Parse a statement"""
        self.skip_newlines()

        if self.match(TokenType.EOF):
            return None

        # Cognitive variable: <let lvar field_X var_name>
        if self.match(TokenType.LVAR_START):
            return self.parse_cognitive_variable()

        # Let statement: let var = expression;
        elif self.match(TokenType.LET):
            return self.parse_let_statement()

        # If statement: if condition: then_body else: else_body
        elif self.match(TokenType.IF):
            return self.parse_if_statement()

        # Do statement: DO action WITH params;
        elif self.match(TokenType.DO):
            return self.parse_do_statement()

        # Assignment statement: var = expression (without 'let' keyword)
        elif self.match(TokenType.ID) and self.peek_token().type == TokenType.ASSIGN:
            return self.parse_assignment_statement()

        # Expression statement (function calls, etc.)
        else:
            expr = self.parse_expression()
            self.skip_semicolon()
            return ExprStmt(expr)

    def parse_cognitive_variable(self) -> CogVar:
        """Parse cognitive variable: <let lvar field_X var_name>content<lvar/>"""
        self.expect(TokenType.LVAR_START)  # <let lvar

        # Parse field_X
        field_token = self.expect(TokenType.FIELD)
        field_type = field_token.value.replace("field_", "")

        # Parse variable name (no 'as' keyword in natural syntax)
        var_token = self.expect(TokenType.ID)
        variable_name = var_token.value

        # Expect '>' to close opening tag
        if self.current_token().value == ">":
            self.advance()

        # Read content until <lvar/>
        content = ""
        while not self.match(TokenType.LVAR_END):
            if self.match(TokenType.EOF):
                raise ParseError("Unclosed cognitive variable", self.current_token())
            # Collect content (this is simplified - in practice you'd handle this better)
            content += self.current_token().value + " "
            self.advance()

        self.expect(TokenType.LVAR_END)  # <lvar/>

        return CogVar(field_type, variable_name, content.strip())

    def parse_let_statement(self) -> Let:
        """Parse let statement: let var = expression;"""
        self.expect(TokenType.LET)

        var_token = self.expect(TokenType.ID)
        variable_name = var_token.value

        self.expect(TokenType.ASSIGN)  # =

        expression = self.parse_expression()

        self.skip_semicolon()

        return Let(variable_name, expression)

    def parse_assignment_statement(self) -> Let:
        """Parse assignment statement: var = expression (without 'let' keyword)"""
        var_token = self.expect(TokenType.ID)
        variable_name = var_token.value

        self.expect(TokenType.ASSIGN)  # =

        expression = self.parse_expression()

        self.skip_semicolon()

        return Let(variable_name, expression)

    def parse_if_statement(self) -> If:
        """Parse if statement: if condition: then_body else: else_body"""
        self.expect(TokenType.IF)

        condition = self.parse_expression()
        self.expect(TokenType.COLON)

        # Parse then body - collect statements until ELSE or end of context
        then_body = []
        while not self.match(TokenType.ELSE, TokenType.EOF):
            self.skip_newlines()

            if self.match(TokenType.ELSE, TokenType.EOF):
                break

            stmt = self.parse_statement()
            if stmt:
                then_body.append(stmt)

        # Parse else body (optional)
        else_body = None
        if self.match(TokenType.ELSE):
            self.advance()  # consume ELSE
            self.expect(TokenType.COLON)

            else_body = []
            # Parse else body until we hit a context terminator
            while not self.match(TokenType.EOF):
                self.skip_newlines()

                if self.match(TokenType.EOF):
                    break

                stmt = self.parse_statement()
                if stmt:
                    else_body.append(stmt)

        return If(condition, then_body, else_body)

    def parse_do_statement(self) -> Do:
        """Parse do statement: DO action WITH params(*context);"""
        self.expect(TokenType.DO)

        # Parse action name
        action_token = self.expect(TokenType.ID)
        action = action_token.value

        self.expect(TokenType.WITH)

        # Parse parameters
        parameters = []
        if not self.match(TokenType.SEMI):
            parameters = self.parse_argument_list()

        self.skip_semicolon()

        return Do(action, parameters)

    def parse_expression(self) -> Expr:
        """Parse expression with precedence"""
        return self.parse_equality()

    def parse_equality(self) -> Expr:
        """Parse equality expressions: ==, != (lowest precedence)"""
        expr = self.parse_comparison()

        while self.match(TokenType.EQ):
            operator = self.current_token().value
            self.advance()
            right = self.parse_comparison()
            expr = BinaryOp(expr, operator, right)

        return expr

    def parse_comparison(self) -> Expr:
        """Parse relational expressions: >, <, >=, <= (higher precedence)"""
        expr = self.parse_arithmetic()

        while self.match(TokenType.GT, TokenType.LT, TokenType.GTE, TokenType.LTE):
            operator = self.current_token().value
            self.advance()
            right = self.parse_arithmetic()
            expr = BinaryOp(expr, operator, right)

        return expr

    def parse_arithmetic(self) -> Expr:
        """Parse arithmetic expressions: +, - (higher precedence than comparison)"""
        expr = self.parse_primary()

        while self.match(TokenType.PLUS, TokenType.MINUS):
            operator = self.current_token().value
            self.advance()
            right = self.parse_primary()
            expr = BinaryOp(expr, operator, right)

        return expr

    def parse_primary(self) -> Expr:
        """Parse primary expressions"""

        # Variable expansion: *variable
        if self.match(TokenType.STAR):
            self.advance()  # consume *
            var_name = self.expect(TokenType.ID).value
            return Identifier(f"*{var_name}")

        # DO ACTION expressions: DO ACTION function_name(args)
        if self.match(TokenType.DO):
            self.advance()  # consume DO
            self.expect(TokenType.WITH)  # ACTION (mapped to WITH)

            # Parse function call
            func_name = self.expect(TokenType.ID).value
            self.expect(TokenType.LPAREN)
            args = self.parse_argument_list()
            self.expect(TokenType.RPAREN)

            return FuncCall(func_name, args)

        # Numbers
        if self.match(TokenType.NUM):
            value = self.current_token().value
            self.advance()
            # Convert to appropriate type
            if "." in value:
                return Literal(float(value))
            else:
                return Literal(int(value))

        # Strings
        if self.match(TokenType.STR):
            value = self.current_token().value
            self.advance()
            return Literal(value)

        # Semantic operations: similar(), contradicts(), etc.
        if self.match(TokenType.SEM_OP):
            op_name = self.current_token().value
            self.advance()

            self.expect(TokenType.LPAREN)
            args = self.parse_argument_list()
            self.expect(TokenType.RPAREN)

            return SemanticOp(op_name, args)

        # Function calls or identifiers
        if self.match(TokenType.ID):
            name = self.current_token().value
            self.advance()

            # Check if it's a function call
            if self.match(TokenType.LPAREN):
                self.advance()  # consume (
                args = self.parse_argument_list()
                self.expect(TokenType.RPAREN)
                return FuncCall(name, args)
            else:
                return Identifier(name)

        # Parenthesized expressions
        if self.match(TokenType.LPAREN):
            self.advance()
            expr = self.parse_expression()
            self.expect(TokenType.RPAREN)
            return expr

        raise ParseError(
            f"Unexpected token: {self.current_token().value}",
            self.current_token(),
        )

    def parse_argument_list(self) -> list[Expr]:
        """Parse comma-separated argument list"""
        args = []

        if self.match(TokenType.RPAREN, TokenType.SEMI):
            return args

        # Handle *param expansion
        if self.match(TokenType.STAR):
            self.advance()
            args.append(self.parse_expression())
        else:
            args.append(self.parse_expression())

        while self.match(TokenType.COMMA):
            self.advance()  # consume comma

            if self.match(TokenType.STAR):
                self.advance()
                args.append(self.parse_expression())
            else:
                args.append(self.parse_expression())

        return args

    def skip_semicolon(self):
        """Optionally consume semicolon"""
        if self.match(TokenType.SEMI):
            self.advance()


def parse_lndl(source_code: str) -> Program:
    """Parse LNDL source code into AST"""
    lexer = Lexer(source_code)
    tokens = lexer.tokenize()

    parser = Parser(tokens)
    return parser.parse()
