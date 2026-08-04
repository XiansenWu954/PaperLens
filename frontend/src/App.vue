<script setup lang="ts">
import { RouterLink, RouterView } from 'vue-router'
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <RouterLink class="brand" to="/" aria-label="PaperLens home">
        <span class="brand-mark">PL</span>
        <span>
          <strong>PaperLens</strong>
          <small>Literature research workspace</small>
        </span>
      </RouterLink>

      <nav class="topnav" aria-label="Product areas">
        <RouterLink to="/">Projects</RouterLink>
        <RouterLink to="/research">Research Task</RouterLink>
      </nav>
    </header>

    <main class="app-main">
      <RouterView />
    </main>
  </div>
</template>

<style scoped>
.app-shell {
  width: min(1440px, 100%);
  margin: 0 auto;
  padding: 18px 24px 44px;
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  min-height: 56px;
  margin-bottom: 22px;
}

.brand {
  display: inline-flex;
  align-items: center;
  gap: 12px;
  color: var(--text);
  text-decoration: none;
}

.brand-mark {
  width: 38px;
  height: 38px;
  display: grid;
  place-items: center;
  border: 1px solid var(--border-strong);
  border-radius: 7px;
  background: var(--paper);
  color: var(--accent-strong);
  font-family: var(--serif);
  font-weight: 700;
}

.brand strong,
.brand small {
  display: block;
  line-height: 1.2;
}

.brand strong {
  font-size: 17px;
  letter-spacing: 0;
}

.brand small {
  margin-top: 2px;
  color: var(--text-muted);
  font-size: 12px;
}

.topnav {
  display: flex;
  align-items: center;
  gap: 18px;
  font-size: 13px;
}

.topnav a {
  color: var(--text-muted);
  text-decoration: none;
}

.topnav a:hover,
.topnav a.router-link-active {
  color: var(--accent-strong);
}

.app-main {
  min-height: calc(100vh - 120px);
}

@media (max-width: 760px) {
  .app-shell {
    padding: 14px 14px 36px;
  }

  .topnav {
    display: none;
  }
}
</style>
