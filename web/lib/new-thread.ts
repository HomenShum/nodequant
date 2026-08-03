/**
 * The "New Thread" action, held where both the sidebar and the runtime can
 * reach it.
 *
 * Indirection rather than a runtime call, because the runtime calls do not
 * work here and that was established by measurement, not assumption:
 *
 *   ThreadListPrimitive.New          inert without a thread-list runtime
 *   useRemoteThreadListRuntime       runtime state correct (2 messages, 1
 *                                    thread) while the DOM rendered the empty
 *                                    state -- it lives under legacy-runtime/
 *                                    and does not feed the state layer these
 *                                    components read
 *   threads().switchToNewThread()    no-op on a single-thread runtime
 *
 * See web/evidence/threads/FINDINGS.md. The component that owns the messages is
 * the one holding `useChat`, so that component registers how to clear them and
 * the sidebar button invokes it. Small, explicit, and verifiable in the DOM.
 */
export const newThreadAction: { run: () => void } = { run: () => {} };
