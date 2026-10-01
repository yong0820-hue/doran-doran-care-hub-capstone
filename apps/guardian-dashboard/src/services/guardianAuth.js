import { getAuth, onAuthStateChanged, signInWithEmailAndPassword, signOut } from 'firebase/auth'
import { onValue, ref } from 'firebase/database'
import { createGuardianAuthState, guardianBinding } from '../guardianAuthState'
import { getFirebaseApp, getFirebaseDatabase, getFirebaseProjectId, installAuthBoundary, isFirebaseConfigured } from './firebase'

let sdkAuth
const auth = () => {
  if (!isFirebaseConfigured()) throw new Error('AUTH_CONFIGURATION_MISSING')
  return sdkAuth ??= getAuth(getFirebaseApp())
}

export const guardianAuth = createGuardianAuthState({
  currentUid: () => sdkAuth?.currentUser?.uid ?? null,
  watchAuth: (next, error) => onAuthStateChanged(auth(), next, error),
  watchBinding: (uid, next, error) => {
    let active = true
    let version = 0
    let stopScope = null
    const stop = onValue(ref(getFirebaseDatabase(), `accessBindings/${uid}`), (snapshot) => {
      const epoch = ++version
      stopScope?.()
      stopScope = null
      const binding = snapshot.val()
      const scope = guardianBinding(binding)
      if (!scope) { if (active) next(null); return }
      // Clear permission immediately while the administrator-owned scope is
      // checked; a prior scope must not stay usable during this asynchronous read.
      next(undefined)
      if (!active) return
      // A binding cannot authorize an additional device for a single-device
      // elder. Both records are provisioned only by the project administrator.
      stopScope = onValue(ref(getFirebaseDatabase(), `accessScopes/${scope.elderId}`), (owner) => {
        const trusted = owner.val()
        if (active && epoch === version) next(trusted?.deviceId === scope.deviceId
          && trusted.projectId === getFirebaseProjectId() ? binding : null)
      }, () => { if (active && epoch === version) error() })
    }, () => { if (active) error() })
    return () => { active = false; ++version; stop(); stopScope?.() }
  },
  signIn: (email, password) => signInWithEmailAndPassword(auth(), email, password),
  signOut: () => signOut(auth()),
})

installAuthBoundary((elderId, deviceId) => guardianAuth.capture(elderId, deviceId))
