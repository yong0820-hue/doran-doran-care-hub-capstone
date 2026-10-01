// Auth generation owns every subscription and continuation. Bindings are read
// from administrator-written RTDB data; UI input never grants a role or scope.
const key = (value) => typeof value === 'string' && value.length > 0 && value.length <= 128
  && !/[.#$\[\]/\u0000-\u001f\u007f]/u.test(value)

export function guardianBinding(value) {
  if (!value || value.enabled !== true || value.role !== 'guardian'
      || !key(value.elderId) || !key(value.deviceId)) return null
  return Object.freeze({ elderId: value.elderId, deviceId: value.deviceId })
}

export function createGuardianAuthState(io) {
  let state = Object.freeze({ status: 'initializing', uid: null, scope: null, generation: 0, error: '' })
  let generation = 0
  let stopAuth = null
  let stopBinding = null
  let command = 0
  let signingOut = false
  let logoutCompletion = null
  let pendingLogin = null
  let allowRestoredUser = true
  const listeners = new Set()
  const resources = new Set()
  const publish = (next) => {
    state = Object.freeze({ ...next, generation })
    for (const listener of listeners) listener()
  }
  const invalidate = () => {
    generation++
    stopBinding?.()
    stopBinding = null
    for (const stop of [...resources]) { try { stop() } catch { /* invalidated regardless */ } }
    resources.clear()
  }
  const changed = (user) => {
    if (signingOut || (user && (!allowRestoredUser || pendingLogin !== null))) return
    invalidate()
    const epoch = generation
    const uid = user?.uid ?? null
    if (!uid) return publish({ status: 'signed_out', uid: null, scope: null, error: '' })
    publish({ status: 'authorizing', uid, scope: null, error: '' })
    const current = () => generation === epoch && io.currentUid() === uid && !signingOut
    try {
      const unsubscribe = io.watchBinding(uid, (value) => {
        if (!current()) return
        if (value === undefined) {
          if (state.status === 'ready') changed(user)
          else publish({ status: 'authorizing', uid, scope: null, error: '' })
          return
        }
        const scope = guardianBinding(value)
        // Revocation or reassignment invalidates existing continuations before
        // announcing the next state, even when the SDK user itself is unchanged.
        if (state.status === 'ready' && (!scope || scope.elderId !== state.scope.elderId
            || scope.deviceId !== state.scope.deviceId)) {
          changed(user)
          return
        }
        publish({ status: scope ? 'ready' : 'no_access', uid, scope,
          error: scope ? '' : '이 계정에 연결된 보호자 접근 권한이 없습니다.' })
      }, () => {
        if (!current()) return
        invalidate()
        publish({ status: 'no_access', uid, scope: null, error: '접근 권한을 확인하지 못했습니다. 관리자에게 문의해 주세요.' })
      })
      if (generation === epoch) stopBinding = unsubscribe
      else unsubscribe?.()
    } catch {
      if (!current()) return
      invalidate()
      publish({ status: 'no_access', uid, scope: null, error: '접근 권한을 확인하지 못했습니다. 관리자에게 문의해 주세요.' })
    }
  }
  const logout = async () => {
    if (signingOut) return false
    ++command
    allowRestoredUser = false
    signingOut = true
    invalidate()
    publish({ status: 'signing_out', uid: null, scope: null, error: '' })
    try {
      logoutCompletion = Promise.resolve(io.signOut())
      await logoutCompletion
      publish({ status: 'signed_out', uid: null, scope: null, error: '' })
      return true
    } catch {
      publish({ status: 'error', uid: null, scope: null,
        error: '로그아웃을 확인하지 못했습니다. 데이터 접근을 중단했습니다. 다시 로그아웃해 주세요.' })
      return false
    } finally { signingOut = false; logoutCompletion = null }
  }
  return {
    getSnapshot: () => state,
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener) },
    start() {
      if (stopAuth) return
      try {
        stopAuth = io.watchAuth(changed, () => {
          invalidate()
          publish({ status: 'error', uid: null, scope: null, error: '인증 초기화에 실패했습니다. 연결과 설정을 확인해 주세요.' })
        })
      } catch {
        invalidate()
        publish({ status: 'error', uid: null, scope: null, error: '인증 초기화에 실패했습니다. 연결과 설정을 확인해 주세요.' })
      }
    },
    async login(email, password) {
      if (pendingLogin !== null || ['signing_in', 'signing_out'].includes(state.status)) return false
      const attempt = ++command
      pendingLogin = attempt
      allowRestoredUser = true
      invalidate()
      publish({ status: 'signing_in', uid: null, scope: null, error: '' })
      try {
        const result = await io.signIn(email, password)
        if (attempt !== command) {
          // A cancelled SDK sign-in can still restore currentUser/persistence.
          // Finish any earlier logout, then explicitly clear that late session.
          // pendingLogin remains fenced until this cleanup has also completed.
          if (logoutCompletion) { try { await logoutCompletion } catch { /* retry below */ } }
          await logout()
          return false
        }
        pendingLogin = null
        if (result?.user) changed(result.user)
        return true
      } catch {
        if (attempt !== command) return false
        pendingLogin = null
        allowRestoredUser = false
        invalidate()
        publish({ status: 'signed_out', uid: null, scope: null,
          error: '로그인하지 못했습니다. 이메일·비밀번호와 계정 사용 가능 여부를 확인해 주세요.' })
        return false
      } finally {
        // Firebase sign-in cannot be cancelled. Keep a retired attempt fenced
        // until it settles, so a second login cannot race its SDK user update.
        if (pendingLogin === attempt) pendingLogin = null
      }
    },
    logout,
    capture(elderId, deviceId) {
      const owner = state
      const current = () => owner.status === 'ready' && generation === owner.generation
        && state.status === 'ready' && io.currentUid() === owner.uid
        && (!elderId || elderId === owner.scope.elderId)
        && (!deviceId || deviceId === owner.scope.deviceId)
      const assertCurrent = () => { if (!current()) throw new Error('AUTH_SCOPE_CHANGED') }
      assertCurrent()
      return Object.freeze({ uid: owner.uid, generation: owner.generation, scope: owner.scope,
        current, assertCurrent,
        track(stop) {
          if (!current()) { stop(); return () => {} }
          resources.add(stop)
          return () => { resources.delete(stop); stop() }
        },
      })
    },
  }
}
