/* The landing page demo's guided tour: five stops that take a visitor from the
 * term's totals to a recorded payment, its receipt, and the sign-up button.
 *
 * Built on the same vendored Driver.js as the signed-in tour (static/js/tour.js),
 * with the same look (`tour-popover` in assets/app.css). It runs only when the
 * visitor presses "Show me how it works" under the demo's heading; nothing
 * starts on its own.
 *
 * The tour does not reach into the demo. It clicks the demo's own buttons and
 * waits for the visitor to press the ones that matter (Record, then Record and
 * update the balance), so what they see is exactly what the demo does. The one
 * thing it reads is `window.scDemo.owingStatus()`, so it never points at an
 * empty list after the visitor has already settled every balance.
 *
 * Steps find their targets by `data-tour` or by id, never by position. With
 * Driver.js or this file blocked, the start button stays hidden and the demo
 * works as before. Under `prefers-reduced-motion` nothing animates.
 */
(function () {
  'use strict';

  var tour = null;

  function $(selector) { return document.querySelector(selector); }

  // Clicks inside a target advance the tour once, after the demo has reacted
  // (opened its dialog, redrawn its rows). One listener per step, removed when
  // the step changes, so going Back and forth never double-advances.
  var pending = null;
  function advanceOnClick(selector, delay) {
    clearPending();
    var el = $(selector);
    if (!el) return;
    var handler = function (event) {
      if (selector === '#demo-table-body' && !event.target.closest('[data-demo-pay]')) return;
      clearPending();
      window.setTimeout(function () { if (tour && tour.isActive()) tour.moveNext(); }, delay || 120);
    };
    el.addEventListener('click', handler);
    pending = { el: el, handler: handler };
  }
  function clearPending() {
    if (pending) pending.el.removeEventListener('click', pending.handler);
    pending = null;
  }

  function closeDemoDialogs() {
    var open = document.querySelectorAll('#payment-modal [data-demo-close], #receipt-modal [data-demo-close]');
    for (var i = 0; i < open.length; i++) {
      if (!open[i].closest('.hidden')) { open[i].click(); return; }
    }
  }

  function steps() {
    var owing = window.scDemo && window.scDemo.owingStatus ? window.scDemo.owingStatus() : 'PARTIAL';
    var chip = owing === 'UNPAID' ? '#tab-UNPAID' : '#tab-PARTIAL';
    var chipName = owing === 'UNPAID' ? 'Unpaid' : 'Part paid';

    var list = [
      {
        element: '[data-tour="demo-totals"]',
        popover: {
          title: 'This term at a glance',
          description: 'What every student should pay, what has come in and what is still owed, added up for you as payments are recorded.',
          side: 'bottom'
        }
      }
    ];

    if (owing) {
      list.push(
        {
          element: chip,
          popover: {
            title: 'See who still owes',
            description: 'Press <strong>' + chipName + '</strong> to list only the students with a balance.',
            showButtons: ['previous', 'close'],
            side: 'bottom'
          },
          onHighlighted: function () { advanceOnClick(chip, 150); }
        },
        {
          element: '#demo-table-body',
          popover: {
            title: 'Record a payment',
            description: 'A parent has just paid at the bank. Press <strong>Record</strong> next to their child.',
            showButtons: ['previous', 'close'],
            side: 'top'
          },
          onHighlighted: function () { advanceOnClick('#demo-table-body', 150); }
        },
        {
          element: '[data-tour="demo-payment"]',
          popover: {
            title: 'Enter what they paid',
            description: 'The balance is filled in. Change the amount if they paid part of it, then press <strong>Record and update the balance</strong>.',
            showButtons: ['close'],
            side: 'top'
          },
          onHighlighted: function () { advanceOnClick('#demo-submit-payment', 200); }
        },
        {
          element: '#printable-receipt',
          popover: {
            title: 'The receipt is ready',
            description: 'The new balance is on it, and the totals have already moved. Every receipt in SCHOOLCORD comes out like this.',
            showButtons: ['next', 'close'],
            side: 'top'
          }
        }
      );
    }

    list.push({
      element: '[data-tour="demo-signup"]',
      popover: {
        title: 'Do this with your own students',
        description: 'Open a free school account. It is free for schools with up to 100 students.',
        side: 'top',
        doneBtnText: 'Got it'
      },
      onHighlighted: clearPending
    });

    return list;
  }

  function start() {
    var factory = window.driver && window.driver.js && window.driver.js.driver;
    if (!factory) return;
    if (tour && tour.isActive()) tour.destroy();

    var still = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    tour = factory({
      steps: steps(),
      animate: !still,
      smoothScroll: !still,
      overlayColor: '#0f2547',
      overlayOpacity: 0.62,
      allowClose: true,
      showProgress: true,
      progressText: '{{current}} of {{total}}',
      nextBtnText: 'Next',
      prevBtnText: 'Back',
      doneBtnText: 'Done',
      popoverClass: 'tour-popover',
      // The last stop is far down the page: leave the receipt before going there.
      onNextClick: function (el, step, opts) {
        if (step.element === '#printable-receipt') closeDemoDialogs();
        opts.driver.moveNext();
      },
      onDestroyed: function () {
        clearPending();
        closeDemoDialogs();
      }
    });
    tour.drive();
  }

  function init() {
    var button = $('[data-demo-tour-start]');
    if (!button || !$('#live-demo')) return;
    if (!(window.driver && window.driver.js && window.driver.js.driver)) return;
    button.hidden = false;
    button.addEventListener('click', start);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
