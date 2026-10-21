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

  function setFieldError(field, message) {
    const input = form.elements[field];
    if (input) {
      input.setAttribute('aria-invalid', 'true');
    }
    const err = document.getElementById('error-' + field);
    if (err) {
      err.textContent = message;
    }
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
    const password = form.elements['password']?.value || '';

    if (isRegister) {
      const confirmPw = form.elements['confirm_password']?.value || '';
      if (password && confirmPw !== password) {
        setFieldError('confirm_password', 'Passwords do not match');
        form.elements['confirm_password']?.focus();
        return;
      }
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
          username: (form.elements['username'].value || '').trim().toLowerCase(),
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
        setAlert(wait > 0 ? `Too many attempts, try again in ${wait}s` : 'Too many attempts, try again shortly');
      } else if (res.status === 422) {
        let body = null;
        try {
          body = await res.json();
        } catch {
          body = null;
        }
        if (body && Array.isArray(body.errors) && body.errors.length > 0) {
          let fieldFound = false;
          body.errors.forEach(function (err) {
            if (err.field) {
              setFieldError(err.field, err.message);
              if (!firstInvalid && form.elements[err.field]) {
                firstInvalid = form.elements[err.field];
              }
              fieldFound = true;
            }
          });
          if (firstInvalid) firstInvalid.focus();
          if (!fieldFound) setAlert(body.message || 'Validation failed');
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
