### Security

- The `deny_read` entries of `child_sandbox` in `~/.lionagi/settings.yaml`, and of its `extends`
  files, now reach the two sandbox policies lionagi builds itself: the API write set's bash and
  editor, and the confined gemini-cli process. Before, both took only the built-in credential
  list, so a path the user added there was denied to a Claude Code child and still readable to
  those two. They take the list a Claude Code child launched without a profile is denied, with no
  profile's `allow_read` exemption, and `enabled: false` does not shorten it there. A malformed
  `child_sandbox` block now refuses their launch too, with no opt-out in the message.
