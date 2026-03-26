_silence_states: dict[int, bool] = {}


def is_silent(group_id: int) -> bool:
    return _silence_states.get(group_id, False)


def set_silent(group_id: int, silent: bool) -> None:
    if silent:
        _silence_states[group_id] = True
        return

    _silence_states.pop(group_id, None)


def reset_silence_states() -> None:
    _silence_states.clear()
