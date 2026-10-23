$(document).ready(function() {
    $('.dropdown-toggle').dropdown();
    $('.alert').fadeTo(3000, 500).slideUp(500, function(){
        $('.alert').slideUp(500);
    });
});