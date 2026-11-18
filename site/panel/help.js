(function(){
  var h=document.documentElement;
  function set(l){h.setAttribute('data-l',l);try{localStorage.setItem('ri-help-lang',l)}catch(e){}}
  try{var s=localStorage.getItem('ri-help-lang');if(s==='tr'||s==='ar')set(s)}catch(e){}
  [].forEach.call(document.querySelectorAll('[data-set]'),function(b){b.addEventListener('click',function(){set(b.getAttribute('data-set'))})});
})();
