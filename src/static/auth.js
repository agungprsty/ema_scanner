(function () {
  'use strict';

  const loginForm = document.getElementById('login-form');
  const registerForm = document.getElementById('register-form');
  const form = loginForm || registerForm;
  if (!form) return;

  const isRegister = Boolean(registerForm);
  const alertEl = document.getElementById('form-alert');
  const submitBtn = form.querySelector('button[type="submit"]');
  const initialBtnText = submitBtn ? submitBtn.textContent : 'Submit';

  // Toggle password visibility
  const showPwCheckbox = document.getElementById('show-password');
  if (showPwCheckbox) {
    showPwCheckbox.addEventListener('change', function () {
      const pws = form.querySelectorAll('input[name="password"], input[name="confirm_password"]');
      pws.forEach(function (input) {
        input.type = showPwCheckbox.checked ? 'text' : 'password';
      });
    });
  }

  function clearErrors() {
    if (alertEl) {
      alertEl.textContent = '';
      alertEl.style.display = 'none';
    }
    const inputs = form.querySelectorAll('input');
    inputs.forEach(function (inp) {
      inp.removeAttribute('aria-invalid');
      const err = document.getElementById('error-' + inp.name);
      if (err) err.textContent = '';
    });
  }

  // Returns the input when the field exists on this form, so callers can focus it or fall back to the banner
  function setFieldError(field, message) {
    const input = form.elements[field];
    const err = document.getElementById('error-' + field);
    if (!input || !err) return null;
    input.setAttribute('aria-invalid', 'true');
    err.textContent = message;
    return input;
  }

  // Same rules as RegisterRequest / LoginRequest in src/main.py
  const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
  const USERNAME_RE = /^[A-Za-z0-9_]{3,32}$/;

  // Pydantic counts code points, JS .length counts UTF-16 units (emoji count twice)
  function charCount(text) {
    return [...text].length;
  }

  function validate(values) {
    const errors = [];
    if (!EMAIL_RE.test(values.email) || charCount(values.email) > 254) {
      errors.push(['email', 'Enter a valid email address']);
    }
    if (!isRegister) {
      if (!values.password || charCount(values.password) > 128) errors.push(['password', 'Enter your password']);
      return errors;
    }
    if (!USERNAME_RE.test(values.username)) {
      errors.push(['username', '3-32 characters: letters, numbers or underscore']);
    }
    const pw = values.password;
    if (charCount(pw) < 10 || charCount(pw) > 128) {
      errors.push(['password', 'Password must be 10 to 128 characters']);
    } else if (pw.toLowerCase() === values.email || pw.toLowerCase() === values.username) {
      errors.push(['password', 'Password cannot match email or username']);
    }
    if (values.confirm !== pw) {
      errors.push(['confirm_password', 'Passwords do not match']);
    }
    return errors;
  }

  function setAlert(message) {
    if (alertEl) {
      alertEl.textContent = message;
      alertEl.style.display = 'block';
    }
  }

  form.addEventListener('submit', async function (e) {
    e.preventDefault();
    clearErrors();
    let firstInvalid = null;

    const email = (form.elements['email']?.value || '').trim().toLowerCase();
    const username = (form.elements['username']?.value || '').trim().toLowerCase();
    const password = form.elements['password']?.value || '';
    const confirm = form.elements['confirm_password']?.value || '';

    const clientErrors = validate({ email: email, username: username, password: password, confirm: confirm });
    if (clientErrors.length) {
      clientErrors.forEach(function (pair) {
        const input = setFieldError(pair[0], pair[1]);
        if (!firstInvalid) firstInvalid = input;
      });
      if (firstInvalid) firstInvalid.focus();
      return;
    }

    // Submit request
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.textContent = isRegister ? 'Creating account...' : 'Signing in...';
    }

    const endpoint = isRegister ? '/api/auth/register' : '/api/auth/login';
    const payload = isRegister
      ? {
          email: email,
          username: username,
          password: password,
        }
      : {
          email: email,
          password: password,
        };

    try {
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
      });

      if (res.ok) {
        window.location.assign('/dashboard');
        return;
      }

      if (res.status === 401) {
        setAlert('Invalid email or password');
      } else if (res.status === 409) {
        setAlert('Email or username is already registered');
      } else if (res.status === 403) {
        setAlert('Request rejected');
      } else if (res.status === 429) {
        const wait = Number(res.headers.get('Retry-After'));
        setAlert(wait > 0 ? `Too many attempts, try again in ${wait} s` : 'Too many attempts, try again shortly');
      } else if (res.status === 422) {
        let body = null;
        try {
          body = await res.json();
        } catch {
          body = null;
        }
        if (body && Array.isArray(body.errors) && body.errors.length > 0) {
          const unmatched = [];
          body.errors.forEach(function (err) {
            const input = err.field ? setFieldError(err.field, err.message) : null;
            if (!input) unmatched.push(err.message);
            else if (!firstInvalid) firstInvalid = input;
          });
          if (firstInvalid) firstInvalid.focus();
          if (unmatched.length) setAlert(unmatched.join('. ') || body.message || 'Validation failed');
        } else {
          setAlert((body && body.message) || 'Validation failed');
        }
      } else {
        setAlert('Service unavailable, try again shortly');
      }
    } catch {
      setAlert('Network error, please check connection');
    } finally {
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.textContent = initialBtnText;
      }
    }
  });
})();
