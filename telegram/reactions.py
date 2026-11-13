"""Small, explicit reaction-only response protocol; never infer acknowledgments."""
EMOJI = {'👍', '❤', '🔥', '😁', '🎉', '🤔', '👀', '👌', '🙏'}


def emoji_for(text):
    value = text.strip()
    if value.startswith('[[REACT:') and value.endswith(']]'):
        emoji = value[8:-2]
        return emoji if emoji in EMOJI else None
    return None
