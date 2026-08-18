// React 19 requires test environments to opt in before `act()` can flush
// updates without emitting a warning for every render. Individual suites used
// to repeat this flag inconsistently, producing thousands of misleading log
// lines even when their updates were correctly wrapped.
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
