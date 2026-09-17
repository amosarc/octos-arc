import React from 'react';

export function RegisterForm() {
  return (
    <form data-testid="register-form">
      <label htmlFor="name">Name</label>
      <input id="name" type="text" />
      <label htmlFor="password">Password</label>
      <input id="password" type="password" />
      <meter data-testid="password-strength" value="0" min="0" max="4" />
      <button type="submit" data-testid="register-submit">
        Next step
      </button>
    </form>
  );
}
